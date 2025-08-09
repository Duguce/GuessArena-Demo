import configparser
import json
import os
import random
import logging
import datetime
import hashlib
import stat
import shutil
import time
import threading
from logging.handlers import RotatingFileHandler
from functools import lru_cache

from flask import Flask, jsonify, redirect, render_template, request, url_for, has_request_context
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from openai import OpenAI
from werkzeug.exceptions import HTTPException

# Setup directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
PROMPTS_DIR = os.path.join(BASE_DIR, 'prompts')

# Ensure directories exist
for directory in [CONFIG_DIR, DATA_DIR]:
    os.makedirs(directory, exist_ok=True)

# Create subdirectories
DATA_CONTENT_DIR = os.path.join(DATA_DIR, 'content')
DATA_LEADERBOARD_DIR = os.path.join(DATA_DIR, 'leaderboard')
DATA_LOGS_DIR = os.path.join(DATA_DIR, 'logs')

for directory in [DATA_CONTENT_DIR, DATA_LEADERBOARD_DIR, DATA_LOGS_DIR]:
    os.makedirs(directory, exist_ok=True)
    # Secure directories - make them not world-writable
    try:
        os.chmod(directory, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)  # 750 permissions
    except Exception as e:
        print(f"Warning: Could not set secure permissions on {directory}: {str(e)}")

# Global rate limiting variables - will be configured from settings.json
GLOBAL_REQUEST_COUNT = 0
GLOBAL_REQUEST_WINDOW_START = time.time()
GLOBAL_REQUEST_LOCK = threading.Lock()  # For thread safety
GLOBAL_RATE_LIMIT_ENABLED = False       # Will be updated from settings
GLOBAL_RATE_LIMIT_MAX = None            # Will be updated from settings  
GLOBAL_RATE_LIMIT_WINDOW = None         # Will be updated from settings

app = Flask(__name__)

# Setup security logging
if not os.path.exists(DATA_LOGS_DIR):
    os.makedirs(DATA_LOGS_DIR)

# Create security logger with custom formatter
security_logger = logging.getLogger('security')
security_logger.setLevel(logging.INFO)

# Add rotating file handler for security logs (10 MB per file, max 10 files)
security_log_file = os.path.join(DATA_LOGS_DIR, 'security.log')
security_handler = RotatingFileHandler(security_log_file, maxBytes=10*1024*1024, backupCount=10)
security_formatter = logging.Formatter('%(asctime)s [%(levelname)s] [IP:%(ip)s] %(message)s')
security_handler.setFormatter(security_formatter)
security_logger.addHandler(security_handler)

# Helper function to safely get IP address, even outside request context
def safe_get_ip():
    """Get IP address safely, handling cases outside request context"""
    if has_request_context():
        # Check common proxy headers first
        if request.headers.getlist("X-Forwarded-For"):
            # Take the first IP (original client) in case of multiple proxies
            return request.headers.getlist("X-Forwarded-For")[0].split(',')[0].strip()
        elif request.headers.get("X-Real-IP"):
            return request.headers.get("X-Real-IP")
        elif request.headers.get("CF-Connecting-IP"):  # Cloudflare
            return request.headers.get("CF-Connecting-IP")
        # Fall back to flask-limiter's get_remote_address
        return get_remote_address() or 'unknown'
    else:
        return 'server'  # Indicate this is a server-side event, not a client request

# Create custom filter to add IP address to log records
class IPFilter(logging.Filter):
    def filter(self, record):
        record.ip = safe_get_ip()
        return True

security_logger.addFilter(IPFilter())

# Global rate limiting functions
def reset_global_counter():
    """Reset the global request counter"""
    global GLOBAL_REQUEST_COUNT, GLOBAL_REQUEST_WINDOW_START
    with GLOBAL_REQUEST_LOCK:
        GLOBAL_REQUEST_COUNT = 0
        GLOBAL_REQUEST_WINDOW_START = time.time()

def check_global_rate_limit():
    """Check and update global request counter, return True if limit exceeded"""
    # If global rate limiting is disabled, always return False (not limited)
    if not GLOBAL_RATE_LIMIT_ENABLED:
        return False
    
    global GLOBAL_REQUEST_COUNT, GLOBAL_REQUEST_WINDOW_START
    
    current_time = time.time()
    
    with GLOBAL_REQUEST_LOCK:
        # If current time exceeds the window, reset the counter
        if current_time - GLOBAL_REQUEST_WINDOW_START > GLOBAL_RATE_LIMIT_WINDOW:
            GLOBAL_REQUEST_COUNT = 1
            GLOBAL_REQUEST_WINDOW_START = current_time
            return False
        
        # Increment counter and check if limit exceeded
        GLOBAL_REQUEST_COUNT += 1
        if GLOBAL_REQUEST_COUNT > GLOBAL_RATE_LIMIT_MAX:
            return True
    
    return False

# Start a background thread to periodically reset the counter
def start_reset_thread():
    """Start a background thread to periodically reset the global counter"""
    # Only start the thread if global rate limiting is enabled
    if not GLOBAL_RATE_LIMIT_ENABLED:
        return
    
    def reset_periodically():
        while True:
            time.sleep(GLOBAL_RATE_LIMIT_WINDOW)
            reset_global_counter()
    
    reset_thread = threading.Thread(target=reset_periodically, daemon=True)
    reset_thread.start()

# Global request limiting middleware
@app.before_request
def global_rate_limiting():
    """Check global rate limit before processing request"""
    # Skip if global rate limiting is disabled
    if not GLOBAL_RATE_LIMIT_ENABLED:
        return
    
    if check_global_rate_limit():
        log_security_event('global_rate_limit_exceeded', 'Global rate limit exceeded', logging.WARNING, extra={
            'current_count': GLOBAL_REQUEST_COUNT,
            'limit': GLOBAL_RATE_LIMIT_MAX,
            'window': GLOBAL_RATE_LIMIT_WINDOW
        })
        
        retry_after = int(GLOBAL_RATE_LIMIT_WINDOW - (time.time() - GLOBAL_REQUEST_WINDOW_START))
        return jsonify({
            "error": "Global rate limit exceeded. The server is currently processing too many requests.",
            "retry_after": retry_after if retry_after > 0 else GLOBAL_RATE_LIMIT_WINDOW
        }), 429

# Track suspicious activities
suspicious_ips = {}

# Define default suspicious threshold and window before actual settings are loaded
DEFAULT_SUSPICIOUS_THRESHOLD = 10
DEFAULT_SUSPICIOUS_WINDOW = 30 * 60  # 30 minutes in seconds

def log_security_event(event_type, message, level=logging.INFO, extra=None):
    """Log a security event with consistent formatting"""
    log_data = {'event_type': event_type}
    if extra:
        log_data.update(extra)
    
    ip = safe_get_ip()
    
    # Track potentially suspicious activities - only for actual client requests
    if level >= logging.WARNING and ip != 'server':
        current_time = datetime.datetime.now()
        if ip not in suspicious_ips:
            suspicious_ips[ip] = {'count': 0, 'first_seen': current_time, 'events': []}
        
        suspicious_ips[ip]['count'] += 1
        suspicious_ips[ip]['events'].append({'time': current_time, 'event': message})
        
        # Get thresholds from global variables - will be updated after settings are loaded
        suspicious_threshold = getattr(globals(), 'SUSPICIOUS_THRESHOLD', DEFAULT_SUSPICIOUS_THRESHOLD)
        suspicious_window = getattr(globals(), 'SUSPICIOUS_WINDOW', DEFAULT_SUSPICIOUS_WINDOW)
        
        # Clean up old records
        for tracked_ip in list(suspicious_ips.keys()):
            if (current_time - suspicious_ips[tracked_ip]['first_seen']).total_seconds() > suspicious_window:
                del suspicious_ips[tracked_ip]
        
        # Log if threshold exceeded
        if suspicious_ips[ip]['count'] >= suspicious_threshold:
            security_logger.critical(
                f"SECURITY ALERT: IP has triggered {suspicious_ips[ip]['count']} suspicious events",
                extra={'ip': ip}
            )
    
    security_logger.log(level, f"{message} | {json.dumps(log_data)}", extra={'ip': ip})

# Security utilities for safe file access
def is_safe_path(base_dir, requested_path):
    """Validate that the requested path is within the base directory"""
    # Canonicalize paths to handle ../ and symlinks
    base_dir_abs = os.path.abspath(base_dir)
    requested_path_abs = os.path.abspath(requested_path)
    # Check if the normalized path starts with the base directory
    return requested_path_abs.startswith(base_dir_abs)

def validate_file_permissions(filepath):
    """Check if the file has secure permissions (not world-readable)"""
    if not os.path.exists(filepath):
        return False
    
    # Get file stats
    file_stats = os.stat(filepath)
    
    # Check if file is world-readable (permissions end with 4 or more)
    world_readable = bool(file_stats.st_mode & stat.S_IROTH)
    
    # On Unix/Linux, check if directory permissions are secure
    parent_dir = os.path.dirname(filepath)
    dir_stats = os.stat(parent_dir)
    dir_world_writable = bool(dir_stats.st_mode & stat.S_IWOTH)
    
    return not (world_readable or dir_world_writable)

def calculate_file_hash(filepath):
    """Calculate SHA-256 hash of a file for integrity verification"""
    if not os.path.exists(filepath):
        return None
    
    try:
        with open(filepath, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()
        return file_hash
    except Exception:
        return None

def secure_read_file(base_dir, relative_path, verify_permissions=True):
    """Securely read a file with path and permission validation"""
    # Construct full path and secure it
    full_path = os.path.join(base_dir, relative_path)
    
    # Validate path is within allowed directory
    if not is_safe_path(base_dir, full_path):
        log_security_event('security_violation', f'Path traversal attempt: {relative_path}', 
                          logging.WARNING)
        raise ValueError(f"Invalid file path: {relative_path}")
    
    # Check file permissions if requested
    if verify_permissions and not validate_file_permissions(full_path):
        log_security_event('security_violation', f'Insecure file permissions: {relative_path}', 
                          logging.WARNING)
        
        # Try to fix permissions if possible
        try:
            # Make file readable only by owner and group
            os.chmod(full_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        except Exception as e:
            log_security_event('security_error', f'Failed to fix file permissions: {str(e)}', 
                             logging.ERROR)
    
    # Read and return file content
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        log_security_event('file_error', f'Error reading file {relative_path}: {str(e)}', 
                          logging.ERROR)
        raise

def secure_json_load(base_dir, relative_path, default=None):
    """Securely load JSON file with path validation and proper error handling"""
    try:
        content = secure_read_file(base_dir, relative_path)
        return json.loads(content)
    except (ValueError, json.JSONDecodeError) as e:
        log_security_event('file_error', f'Error parsing JSON file {relative_path}: {str(e)}', 
                          logging.ERROR)
        return default if default is not None else {}
    except Exception as e:
        log_security_event('file_error', f'Unexpected error with file {relative_path}: {str(e)}', 
                          logging.ERROR)
        return default if default is not None else {}

# Load application settings with enhanced security
SETTINGS_PATH = os.path.join(CONFIG_DIR, 'settings.json')
try:
    # Use secure JSON loader
    relative_settings_path = os.path.relpath(SETTINGS_PATH, BASE_DIR)
    SETTINGS = secure_json_load(BASE_DIR, relative_settings_path)
    print("Settings loaded successfully from", SETTINGS_PATH)
    
    # Calculate and store file hash for integrity checks
    settings_hash = calculate_file_hash(SETTINGS_PATH)
    if settings_hash:
        SETTINGS_INTEGRITY = {"path": SETTINGS_PATH, "hash": settings_hash, "time": datetime.datetime.now()}
    
except Exception as e:
    print(f"Warning: Could not load settings from {SETTINGS_PATH}: {e}")
    # Default settings if the file doesn't exist or is invalid
    SETTINGS = {
        "application": {"name": "GuessArena-Demo", "version": "1.0.0", "debug": True},
        "game": {"max_answers": 30, "default_model": "Qwen2.5-14B-Instruct"},
        "security": {
            "suspicious_threshold": 10,
            "suspicious_window_minutes": 30,
            "rate_limits": {
                "default": "100 per minute, 1000 per hour, 2000 per day",
                "play_api": "30 per minute", 
                "ai_simulation_api": "15 per minute",
                "web_pages": "50 per minute"
            },
            "global_limits": {
                "max_requests": 1000,  # Maximum requests per window
                "window_seconds": 60,   # Window time in seconds
                "enabled": True         # Whether global rate limiting is enabled
            }
        },
        "paths": {
            "prompts": "prompts",
            "logs": "data/logs",
            "leaderboard": "data/leaderboard"
        }
    }

# Load global rate limit settings from configuration
global_limits = SETTINGS.get("security", {}).get("global_limits", {})
GLOBAL_RATE_LIMIT_ENABLED = global_limits.get("enabled", False)
GLOBAL_RATE_LIMIT_MAX = global_limits.get("max_requests", 1000)
GLOBAL_RATE_LIMIT_WINDOW = global_limits.get("window_seconds", 60)

# Start the reset thread when the application starts if global rate limiting is enabled
if GLOBAL_RATE_LIMIT_ENABLED:
    start_reset_thread()

# Use values from settings
SUSPICIOUS_THRESHOLD = SETTINGS["security"]["suspicious_threshold"] 
SUSPICIOUS_WINDOW = SETTINGS["security"]["suspicious_window_minutes"] * 60  # Convert minutes to seconds

# Initialize rate limiter with settings
rate_limit_defaults = SETTINGS["security"]["rate_limits"]["default"].split(", ")
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=rate_limit_defaults,
    storage_uri="memory://",  # Using in-memory storage for simplicity
    strategy="fixed-window",  # Simple fixed window strategy
)

# Configure security headers
@app.after_request
def add_security_headers(response):
    """Add security headers to every response."""
    # Content Security Policy (CSP)
    csp = {
        'default-src': ["'self'"],
        'script-src': ["'self'", "'unsafe-inline'"],  # Unsafe-inline needed for embedded scripts
        'style-src': ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
        'font-src': ["'self'", "https://fonts.gstatic.com"],
        'img-src': ["'self'", "data:"],
        'connect-src': ["'self'"],
        'frame-ancestors': ["'none'"],
        'form-action': ["'self'"],
        'base-uri': ["'self'"],
        'object-src': ["'none'"]
    }
    
    # Build the CSP header string
    csp_string = '; '.join([f"{key} {' '.join(value)}" for key, value in csp.items()])
    
    # Add security headers
    response.headers['Content-Security-Policy'] = csp_string
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Add crawler control headers
    if request.path.startswith('/play') or request.path.startswith('/ai_simulation'):
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    elif request.path == '/' or request.path == '/leaderboard':
        response.headers['X-Robots-Tag'] = 'index, follow'
    else:
        response.headers['X-Robots-Tag'] = 'noindex, follow'
    
    # Only add HSTS in production environment
    if not app.debug:
        # HTTP Strict Transport Security (max-age of 1 year in seconds)
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    # Add feature policy to restrict certain browser features
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    
    # Log headers being set for debugging purposes
    if app.debug:
        log_security_event('security_headers', 'Security headers added to response', extra={
            'path': request.path,
            'method': request.method,
            'status_code': response.status_code
        })
    
    return response

# Configure message for when rate limit is exceeded
app.config["RATELIMIT_HEADER_ENABLED"] = True
app.config["RATELIMIT_HEADER_RETRY_AFTER_VALUE"] = "delta-seconds"


# Define custom error handler for rate limiting
@app.errorhandler(429)
def ratelimit_handler(e):
    log_security_event('rate_limit_exceeded', 'Rate limit exceeded', logging.WARNING, extra={
        'path': request.path,
        'method': request.method,
        'user_agent': request.headers.get('User-Agent', 'unknown')
    })
    
    return (
        jsonify(
            {
                "error": "Rate limit exceeded. Please slow down your requests.",
                "retry_after": e.description,
            }
        ),
        429,
    )


# Load models configuration
config = configparser.ConfigParser()
# Update path to store models.ini directly in CONFIG_DIR
MODELS_CONFIG_PATH = os.path.join(CONFIG_DIR, 'models.ini')

# Handle configuration files from old locations
CONFIG_APP_DIR = os.path.join(CONFIG_DIR, 'app')
APP_MODELS_CONFIG_PATH = os.path.join(CONFIG_APP_DIR, 'models.ini')

# Load configuration file
if os.path.exists(MODELS_CONFIG_PATH):
    config.read(MODELS_CONFIG_PATH)
    print(f"Loaded model configuration from {MODELS_CONFIG_PATH}")
else:
    print(f"Warning: Could not find model configuration at {MODELS_CONFIG_PATH}")

# Load cards configuration
DECKS_CONFIG_PATH = os.path.join(DATA_CONTENT_DIR, 'decks.json')

if os.path.exists(DECKS_CONFIG_PATH):
    with open(DECKS_CONFIG_PATH, "r", encoding="utf-8") as f:
        industry_cards = json.load(f)
else:
    industry_cards = {}

if industry_cards:
    default_industry = list(industry_cards.keys())[0]
    DEFAULT_DECK = industry_cards[default_industry]
    DEFAULT_CHOSEN_CARD = random.choice(DEFAULT_DECK) if DEFAULT_DECK else "NO_CARD"
else:
    DEFAULT_DECK = []
    DEFAULT_CHOSEN_CARD = "NO_CARD"

# Define prompt paths based on settings
JUDGE_PROMPT_PATH = os.path.join(BASE_DIR, SETTINGS["paths"]["prompts"], "judge_prompt.txt")
TESTEE_PROMPT_PATH = os.path.join(BASE_DIR, SETTINGS["paths"]["prompts"], "testee_prompt.txt")

AVAILABLE_MODELS = {section: dict(config[section]) for section in config.sections()}
DEFAULT_MODEL = SETTINGS["game"]["default_model"]

# Use max_answers from settings
MAX_ANSWERS = SETTINGS["game"]["max_answers"]

# Updated HTML formatting instead of Markdown for proper rendering
GAME_RULES_PLAYER = (
    "🎴 <strong>Card Guessing Game Rules</strong> 🎴<br>"
    "1️⃣ Ask <strong>yes/no questions</strong> about the cards in the deck.<br>"
    "2️⃣ The <strong>Judge AI</strong> will evaluate and respond.<br>"
    "3️⃣ If you guess the correct card, the game ends.<br>"
    f"4️⃣ You have <strong>{MAX_ANSWERS} attempts</strong>. Exceeding this limit reveals the correct card.<br>"
    "5️⃣ Choose a <strong>predefined deck</strong> or create a <strong>custom deck</strong>.<br>"
    "🎯 Try to guess the card in as few questions as possible!"
)

GAME_RULES_SIMULATION = (
    "🤖 <strong>AI Simulation Rules</strong> 🤖<br>"
    "1️⃣ The <strong>Testee Model</strong> asks yes/no questions.<br>"
    "2️⃣ The <strong>Judge Model</strong> evaluates and responds.<br>"
    f"3️⃣ The game ends when the correct card is guessed or attempts are reached.<br>"
    "4️⃣ Choose models, deck type (predefined/custom), and max rounds (5, 10, 20, 30).<br>"
    "📊 Use this mode to evaluate AI performance!"
)


def validate_custom_deck(deck):
    """Validate that a custom deck meets requirements.
    
    Args:
        deck: The deck to validate
        
    Returns:
        bool: True if valid, False otherwise
    """
    if not isinstance(deck, list):
        log_security_event('validation_error', 'Invalid deck: not a list', logging.WARNING, extra={
            'deck_type': type(deck).__name__
        })
        return False
    
    if len(deck) > 50:
        log_security_event('validation_error', f'Invalid deck: too many cards ({len(deck)})', logging.WARNING)
        return False
        
    for card in deck:
        if not isinstance(card, str) or len(card) > 50:
            log_security_event('validation_error', 'Invalid card in deck', logging.WARNING, extra={
                'card_type': type(card).__name__,
                'card_length': len(card) if isinstance(card, str) else 0
            })
            return False
            
    return True


def parse_chosen_card(system_content: str) -> str:
    """Extract the chosen card from system content more efficiently."""
    if not system_content:
        return "UNKNOWN_CARD"
    start_marker = "[CHOSEN_CARD="
    start_idx = system_content.find(start_marker)
    if (start_idx == -1):
        return "UNKNOWN_CARD"
    start_idx += len(start_marker)
    end_idx = system_content.find("]", start_idx)
    return (
        system_content[start_idx:end_idx].strip() if end_idx != -1 else "UNKNOWN_CARD"
    )


def get_current_deck(card_set_type, user_industry=None, custom_deck=None):
    """Helper function to determine the current deck and chosen card."""
    if card_set_type == "custom" and custom_deck:
        # Validate custom deck before using
        if not validate_custom_deck(custom_deck):
            return DEFAULT_DECK, DEFAULT_CHOSEN_CARD
        return custom_deck, random.choice(custom_deck) if custom_deck else "NO_CARD"

    if user_industry in industry_cards:
        deck = industry_cards[user_industry]
        return deck, random.choice(deck) if deck else "NO_CARD"

    return DEFAULT_DECK, DEFAULT_CHOSEN_CARD


@app.route("/")
@limiter.limit(SETTINGS["security"]["rate_limits"]["web_pages"])  # Use rate limit from settings
def index():
    log_security_event('page_access', 'Home page accessed', extra={
        'user_agent': request.headers.get('User-Agent', 'unknown')
    })
    return render_template(
        "player_arena.html",
        models=AVAILABLE_MODELS.keys(),
        deck=DEFAULT_DECK,
        rules=GAME_RULES_PLAYER,
        industries=industry_cards.keys(),
    )


@app.route("/simulate")
@limiter.limit(SETTINGS["security"]["rate_limits"]["web_pages"])
def simulate():
    return render_template(
        "ai_arena.html",
        models=AVAILABLE_MODELS.keys(),
        deck=DEFAULT_DECK,
        rules=GAME_RULES_SIMULATION,
        industries=industry_cards.keys(),
    )


@app.route("/config", methods=["GET"])
@lru_cache(maxsize=1)
@limiter.limit(SETTINGS["security"]["rate_limits"]["default"].split(", ")[0])  # Use the first rate limit
def get_config():
    """Return game configuration, cached for performance."""
    # Return more complete config info
    return jsonify({
        "max_answers": MAX_ANSWERS,
        "game_version": SETTINGS["application"]["version"],
        "default_model": DEFAULT_MODEL
    })


@app.route("/deck", methods=["GET"])
@limiter.limit(SETTINGS["security"]["rate_limits"]["default"].split(", ")[0])
def get_deck():
    """Return the deck for a given industry."""
    industry = request.args.get("industry", "")

    if industry in industry_cards:
        app.logger.debug(f"Fetching deck for industry: {industry}")
        return jsonify({"deck": industry_cards[industry]})
    else:
        app.logger.debug(f"Industry '{industry}' not found, using default deck")
        return jsonify({"deck": DEFAULT_DECK})


@app.route("/play", methods=["POST"])
@limiter.limit(SETTINGS["security"]["rate_limits"]["play_api"])  # Use rate limit from settings
def play():
    data = request.json
    user_message = data.get("message", "")
    model_key = data.get("model", DEFAULT_MODEL)
    message_log = data.get("message_log", [])
    user_industry = data.get("industry", "")
    card_set_type = data.get("card_set_type", "predefined")
    chosen_card_manual = data.get("chosen_card", "").strip()
    
    log_security_event('api_access', 'Play API accessed', extra={
        'model': model_key,
        'card_set_type': card_set_type,
        'message_length': len(user_message) if user_message else 0
    })
    
    # Validate custom deck if provided
    custom_deck = data.get("custom_deck", [])
    if card_set_type == "custom" and not validate_custom_deck(custom_deck):
        log_security_event('validation_error', 'Invalid custom deck submission', logging.WARNING, extra={
            'deck_length': len(custom_deck) if isinstance(custom_deck, list) else 0
        })
        return jsonify({"error": "Invalid custom deck. Ensure it's a list with max 50 cards, each a string with max 50 characters."}), 400

    # Validate model
    if model_key not in AVAILABLE_MODELS:
        log_security_event('validation_error', f'Invalid model requested: {model_key}', logging.WARNING)
        return jsonify({"error": f"Invalid model: {model_key}"}), 400

    # Check if maximum answers reached
    assistant_count = sum(1 for m in message_log if m["role"] == "assistant")
    if assistant_count >= MAX_ANSWERS:
        chosen_card_now = "DEFAULT_CHOSEN_CARD"
        if message_log and message_log[0]["role"] == "system":
            chosen_card_now = parse_chosen_card(message_log[0]["content"])
        return (
            jsonify(
                {
                    "error": "You have reached the maximum answer limit.",
                    "max_answers": MAX_ANSWERS,
                    "correct_card": chosen_card_now,
                }
            ),
            403,
        )

    current_deck, current_chosen_card = get_current_deck(
        card_set_type, user_industry, data.get("custom_deck", [])
    )

    # Use manually specified card if provided and valid
    if chosen_card_manual and chosen_card_manual in current_deck:
        current_chosen_card = chosen_card_manual

    if not message_log or message_log[0].get("role") != "system":
        if os.path.exists(JUDGE_PROMPT_PATH):
            with open(JUDGE_PROMPT_PATH, "r", encoding="utf-8") as f:
                judge_template = f.read()
            system_prompt_content = judge_template.format(
                deck_of_cards=current_deck, chosen_card=current_chosen_card
            )
        else:
            system_prompt_content = "You are a helpful assistant (fallback prompt)."
        system_prompt_content += f"\n[CHOSEN_CARD={current_chosen_card}]"
        message_log.insert(0, {"role": "system", "content": system_prompt_content})

    chosen_card_now = parse_chosen_card(message_log[0]["content"])

    message_log.append({"role": "user", "content": user_message})

    try:
        model_info = AVAILABLE_MODELS[model_key]
        client = OpenAI(base_url=model_info["base_url"], api_key=model_info["api_key"])
        response = client.chat.completions.create(
            model=model_info["model"], messages=message_log
        )
        bot_response = response.choices[0].message.content
        message_log.append({"role": "assistant", "content": bot_response})
    except Exception as e:
        log_security_event('api_error', f'Error in OpenAI API call: {str(e)}', logging.ERROR, extra={
            'model': model_key,
            'error_type': type(e).__name__
        })
        bot_response = f"Error: {str(e)}"

    game_over = False
    win_message = ""
    if "[end]" in bot_response.lower():
        game_over = True
        if chosen_card_now.strip().lower() in user_message.strip().lower():
            win_message = f"🎉 Congratulations, you guessed correctly! The chosen card is {chosen_card_now}."
        else:
            win_message = f"🔍 Unfortunately, that's not correct. The chosen card is {chosen_card_now}."
    else:
        new_assistant_count = assistant_count + 1
        if new_assistant_count == MAX_ANSWERS:
            game_over = True
            message_log[-1]["content"] = bot_response
            win_message = f"You have used all {MAX_ANSWERS} questions. The correct card is {chosen_card_now}."
    return jsonify(
        {
            "response": bot_response,
            "model_used": model_info["model"],
            "message_log": message_log,
            "game_over": game_over,
            "win_message": win_message,
        }
    )


@app.route("/ai_simulation", methods=["POST"])
@limiter.limit(SETTINGS["security"]["rate_limits"]["ai_simulation_api"])  # Use rate limit from settings
def ai_simulation():
    data = request.json or {}
    judge_key = data.get("judge_model", DEFAULT_MODEL)
    testee_key = data.get("testee_model", DEFAULT_MODEL)
    card_set_type = data.get("card_set_type", "predefined")
    chosen_card_manual = data.get("chosen_card", "").strip()
    max_rounds_input = data.get("max_rounds", MAX_ANSWERS)
    
    log_security_event('api_access', 'AI Simulation API accessed', extra={
        'judge_model': judge_key,
        'testee_model': testee_key,
        'card_set_type': card_set_type
    })
    
    # Validate custom deck if provided
    custom_deck = data.get("custom_deck", [])
    if card_set_type == "custom" and not validate_custom_deck(custom_deck):
        return jsonify({"error": "Invalid custom deck. Ensure it's a list with max 50 cards, each a string with max 50 characters."}), 400
    
    try:
        max_rounds = int(max_rounds_input)
    except (ValueError, TypeError):
        max_rounds = MAX_ANSWERS

    if max_rounds not in [5, 10, 20, 30]:
        max_rounds = 10

    if judge_key not in AVAILABLE_MODELS:
        return jsonify({"error": f"Invalid judge model: {judge_key}"}), 400

    if testee_key not in AVAILABLE_MODELS:
        return jsonify({"error": f"Invalid testee model: {testee_key}"}), 400

    judge_model_info = AVAILABLE_MODELS[judge_key]
    testee_model_info = AVAILABLE_MODELS[testee_key]

    judge_client = OpenAI(
        base_url=judge_model_info["base_url"], api_key=judge_model_info["api_key"]
    )
    testee_client = OpenAI(
        base_url=testee_model_info["base_url"], api_key=testee_model_info["api_key"]
    )

    current_deck, current_chosen_card = get_current_deck(
        card_set_type, data.get("industry", ""), data.get("custom_deck", [])
    )

    if chosen_card_manual and chosen_card_manual in current_deck:
        current_chosen_card = chosen_card_manual

    if os.path.exists(JUDGE_PROMPT_PATH):
        with open(JUDGE_PROMPT_PATH, "r", encoding="utf-8") as f:
            judge_template = f.read()
        judge_system_content = judge_template.format(
            deck_of_cards=current_deck, chosen_card=current_chosen_card
        )
    else:
        judge_system_content = "You are a helpful assistant (fallback prompt)."

    if os.path.exists(TESTEE_PROMPT_PATH):
        with open(TESTEE_PROMPT_PATH, "r", encoding="utf-8") as f:
            testee_system_content = f.read().format(deck_of_cards=current_deck)
    else:
        testee_system_content = (
            f"You will participate as a player in a card guessing game. "
            f"In front of you is a deck of cards: {current_deck}, and your task is to guess the chosen card. "
            "Please ask your first question."
        )

    judge_messages = [{"role": "system", "content": judge_system_content}]
    testee_messages = [{"role": "system", "content": testee_system_content}]

    game_over = False
    win_message = ""
    assistant_count = 0

    while not game_over and assistant_count < max_rounds:
        try:
            testee_resp = testee_client.chat.completions.create(
                model=testee_model_info["model"], messages=testee_messages
            )
            testee_message = testee_resp.choices[0].message.content
        except Exception as e:
            testee_message = f"[Testee Error: {str(e)}]"
            game_over = True

        testee_messages.append({"role": "assistant", "content": testee_message})
        judge_messages.append({"role": "user", "content": testee_message})

        try:
            judge_resp = judge_client.chat.completions.create(
                model=judge_model_info["model"], messages=judge_messages
            )
            judge_answer = judge_resp.choices[0].message.content
        except Exception as e:
            judge_answer = f"[Judge Error: {str(e)}]"
            game_over = True

        testee_messages.append({"role": "user", "content": judge_answer})
        judge_messages.append({"role": "assistant", "content": judge_answer})

        assistant_count += 1

        if "[end]" in judge_answer.lower():
            game_over = True
            if current_chosen_card.strip().lower() in testee_message.strip().lower():
                win_message = f"🎉 Congratulations, you guessed correctly! The chosen card is {current_chosen_card}."
            else:
                win_message = f"🔍 Unfortunately, that's not correct. The chosen card is {current_chosen_card}."
        elif assistant_count >= max_rounds:
            game_over = True
            win_message = f"You have reached the maximum of {max_rounds} questions. Game over. The chosen card is {current_chosen_card}."
            break
    conversation_log = judge_messages[1:]

    return jsonify(
        {
            "conversation": conversation_log,
            "game_over": game_over,
            "chosen_card": current_chosen_card,
            "max_answers": max_rounds,
            "win_message": win_message,
        }
    )


@app.route("/leaderboard")
@limiter.limit(SETTINGS["security"]["rate_limits"]["web_pages"])
def leaderboard():
    # First try new location from settings
    ranking_data_path = os.path.join(BASE_DIR, SETTINGS["paths"]["leaderboard"], 'rankings.json')
    
    # Handle backward compatibility
    if not os.path.exists(ranking_data_path):
        old_ranking_path = os.path.join(BASE_DIR, 'static', 'data', 'ranking_data.json')
        if os.path.exists(old_ranking_path):
            os.makedirs(os.path.dirname(ranking_data_path), exist_ok=True)
            shutil.copy(old_ranking_path, ranking_data_path)
            
    # If new path exists, use it; otherwise check old location
    if os.path.exists(ranking_data_path):
        with open(ranking_data_path, "r", encoding="utf-8") as f:
            ranking_data = json.load(f)
    else:
        old_ranking_path = os.path.join(BASE_DIR, 'static', 'data', 'ranking_data.json')
        if os.path.exists(old_ranking_path):
            with open(old_ranking_path, "r", encoding="utf-8") as f:
                ranking_data = json.load(f)
        else:
            ranking_data = {"columns": [], "rows": []}

    return render_template("leaderboard.html", ranking_data=ranking_data)


@app.errorhandler(404)
def page_not_found(e):
    """Handle 404 errors by redirecting to the homepage"""
    log_security_event('error', f'404 error: {request.path}', logging.WARNING, extra={
        'user_agent': request.headers.get('User-Agent', 'unknown'),
        'referrer': request.referrer or 'none'
    })
    app.logger.info(f"404 error occurred: {request.path} - Redirecting to homepage")
    return redirect(url_for("index"))


# Add a general error handler
@app.errorhandler(Exception)
def handle_exception(e):
    log_security_event('error', f'Unhandled exception: {str(e)}', logging.ERROR, extra={
        'error_type': type(e).__name__,
        'path': request.path if has_request_context() else 'no_path',
        'method': request.method if has_request_context() else 'no_method'
    })
    
    # Pass through HTTP errors to let Flask handle them
    if isinstance(e, HTTPException):
        return e
    
    # For non-HTTP exceptions, return a 500 error
    return jsonify({"error": "An internal server error occurred"}), 500

if __name__ == "__main__":
    debug_mode = SETTINGS["application"]["debug"]
    global_limit_status = "enabled" if GLOBAL_RATE_LIMIT_ENABLED else "disabled"
    log_security_event('startup', 'Application starting', extra={
        'debug_mode': debug_mode,
        'host': '0.0.0.0',
        'port': 8888,
        'version': SETTINGS["application"]["version"],
        'global_rate_limit': f"{global_limit_status}: {GLOBAL_RATE_LIMIT_MAX} requests per {GLOBAL_RATE_LIMIT_WINDOW} seconds"
    })
    app.run(host="0.0.0.0", port=8888, debug=debug_mode)
