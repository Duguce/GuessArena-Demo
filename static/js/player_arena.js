document.addEventListener("DOMContentLoaded", function() {
    initPlayerArena();
    initCardSetTypeSwitch();
    initCustomCardConfirmation();
});

// Global variables
let messageLog = [];
let maxAnswers = 0;
let assistantCount = 0;
let customDeck = []; // Track custom deck for consistent state

function initPlayerArena() {
    const industrySelect = document.getElementById("industry-select");
    fetchDeckAndUpdate(industrySelect.value);

    // Ensure industry change updates the deck regardless of card set type
    industrySelect.addEventListener("change", function() {
        fetchDeckAndUpdate(this.value);
        // Force predefined set on industry change for better UX
        document.querySelector('input[name="card_set_type"][value="predefined"]').checked = true;
        document.querySelector(".custom-options").style.display = "none";
    });

    document.getElementById("model-select").addEventListener("change", resetGame);
    document.getElementById("send-btn").addEventListener("click", submitQuestion);

    // Add Enter key support
    document.getElementById("user-input").addEventListener("keypress", function(e) {
        if (e.key === "Enter") {
            submitQuestion();
        }
    });

    fetch("/config")
      .then(resp => resp.json())
      .then(data => {
          maxAnswers = data.max_answers;
          updateProgressBar();
      })
      .catch(err => console.error("Error fetching config:", err));
}

function initCardSetTypeSwitch() {
    document.querySelectorAll('input[name="card_set_type"]').forEach(radio => {
        radio.addEventListener("change", function() {
            document.querySelector(".custom-options").style.display = this.value === "custom" ? "flex" : "none";

            // When switching back to predefined set, re-fetch the current industry's deck
            if (this.value === "predefined") {
                const industrySelect = document.getElementById("industry-select");
                if (industrySelect) {
                    fetchDeckAndUpdate(industrySelect.value);
                }
            }
        });
    });
}

function initCustomCardConfirmation() {
    const confirmBtn = document.getElementById("confirm-custom-cards");
    if (confirmBtn) {
        confirmBtn.addEventListener("click", function() {
            const customDeckInput = document.getElementById("custom-deck-input");
            if (customDeckInput && customDeckInput.value.trim()) {
                // Parse the custom deck from input
                customDeck = customDeckInput.value.split(",")
                    .map(card => card.trim())
                    .filter(card => card); // Filter out empty strings

                if (customDeck.length > 0) {
                    // Update the displayed deck
                    updateDeckUI(customDeck);

                    // Reset the game with the new deck
                    resetGame();

                    // Provide user feedback
                    const gameDialogue = document.getElementById("game-dialogue");
                    if (gameDialogue) {
                        displayMessage(gameDialogue, "system-message",
                            `Custom collection confirmed with ${customDeck.length} cards. You may begin your inquiry.`);
                    }
                }
            }
        });
    }
}

function fetchDeckAndUpdate(industry) {
    // Add cache-busting parameter to prevent stale data
    fetch(`/deck?industry=${encodeURIComponent(industry)}&_t=${Date.now()}`)
        .then(resp => {
            if (!resp.ok) {
                throw new Error(`Network response was not ok: ${resp.status}`);
            }
            return resp.json();
        })
        .then(data => {
            if (data && data.deck) {
                console.log(`Received deck for industry ${industry} with ${data.deck.length} cards`);
                updateDeckUI(data.deck);
                resetGame();
            } else {
                console.error("Invalid data format received:", data);
            }
        })
        .catch(err => console.error("Error fetching deck:", err));
}

function updateDeckUI(deck) {
    const deckListElem = document.getElementById("deck-list");
    if (!deckListElem) return;
    deckListElem.innerHTML = "";
    deck.forEach(card => {
        let li = document.createElement("li");
        li.textContent = card;
        deckListElem.appendChild(li);
    });
}

function submitQuestion() {
    const userInputElem = document.getElementById("user-input");
    const gameDialogue = document.getElementById("game-dialogue");
    const userInput = userInputElem.value.trim();
    if (!userInput) return;

    if (assistantCount >= maxAnswers) {
        displayMessage(gameDialogue, "limit-message", `You have used all ${maxAnswers} attempts. Please restart.`);
        return;
    }

    disableInput(true);
    displayMessage(gameDialogue, "user-message", userInput);
    userInputElem.value = "";
    messageLog.push({ role: "user", content: userInput });
    gameDialogue.scrollTop = gameDialogue.scrollHeight;

    const selectedModel = document.getElementById("model-select").value;
    const selectedIndustry = document.getElementById("industry-select").value;
    const cardSetType = document.querySelector('input[name="card_set_type"]:checked').value;

    // Use the tracked customDeck if we're in custom mode
    let deck = cardSetType === "custom" ? customDeck : [];

    // Only read from the textarea if we don't have a confirmed deck or user modified it
    if (cardSetType === "custom" && (deck.length === 0 || document.getElementById("custom-deck-input").value.trim() !== deck.join(", "))) {
        deck = document.getElementById("custom-deck-input").value.split(",").map(s => s.trim()).filter(Boolean);
    }

    let chosenCard = cardSetType === "custom" ? document.getElementById("custom-chosen-card")?.value?.trim() || "" : "";

    fetch("/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            message: userInput,
            model: selectedModel,
            industry: selectedIndustry,
            message_log: messageLog,
            card_set_type: cardSetType,
            custom_deck: deck,
            chosen_card: chosenCard
        })
    })
    .then(resp => resp.json().catch(() => null))
    .then(data => handleResponse(gameDialogue, data))
    .catch(err => {
        console.error("Error:", err);
        disableInput(false);
    });
}

function handleResponse(gameDialogue, data) {
    if (!data) return;

    if (data.error) {
        displayMessage(gameDialogue, "bot-message", `Error: ${data.error} (Max answers = ${data.max_answers})`);
        displayMessage(gameDialogue, "limit-message", `Max attempts reached. The correct card was ${data.correct_card}. Restart the game.`);
        disableInput(true);
        return;
    }

    displayMessage(gameDialogue, "bot-message", `[${data.model_used}] ${data.response}`);
    messageLog = data.message_log;
    assistantCount = messageLog.filter(m => m.role === 'assistant').length;
    updateProgressBar();

    if (data.game_over) {
        displayMessage(gameDialogue, "system-message", data.win_message || "Game Over");
        disableInput(true);
    } else {
        disableInput(assistantCount >= maxAnswers);
    }
    gameDialogue.scrollTop = gameDialogue.scrollHeight;
}

function displayMessage(container, className, text) {
    let msgDiv = document.createElement("div");
    msgDiv.className = className;
    msgDiv.textContent = text;
    container.appendChild(msgDiv);
}

function resetGame() {
    messageLog = [];
    assistantCount = 0;
    const gameDialogue = document.getElementById("game-dialogue");
    if (gameDialogue) {
        gameDialogue.innerHTML = '<div class="system-message">Session reset. Begin a new inquiry session.</div>';
    }
    disableInput(false);
    updateProgressBar();
}

function updateProgressBar() {
    let turnsText = document.getElementById("turns-text");
    let progressBar = document.getElementById("turns-progress-bar");

    if (turnsText) turnsText.textContent = `Responses: ${assistantCount} / ${maxAnswers}`;
    if (progressBar) progressBar.style.width = `${Math.min(100, (assistantCount / maxAnswers) * 100)}%`;
}

function disableInput(disabled) {
    document.getElementById("user-input").disabled = disabled;
    document.getElementById("send-btn").disabled = disabled;
}
