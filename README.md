<h1 align="center">GuessArena Demo</h1>

<p align="center">
  <em>A web-based interactive demo for the GuessArena evaluation framework</em>
</p>

---

> \[!NOTE\]  
> **GuessArena Demo** is a lightweight web application that simulates a card-guessing game with both **player interaction** and **AI-versus-AI simulation**.  
> It provides an intuitive, hands-on interface to explore the evaluation methodology introduced in our paper:  
> <a href="https://aclanthology.org/2025.acl-long.534/">
> <em>“GuessArena: Guess Who I Am? A Self-Adaptive Framework for Evaluating LLMs in Domain-Specific Knowledge and Reasoning”</em>
> </a>.

---

## 🚀 Features

- **Player Mode** – Interactively play the guessing game by asking yes/no questions to an AI judge  
- **AI Simulation Mode** – Observe two LLMs engaging in a self-play guessing process  
- **Leaderboard Tracking** – Compare model performance across different domains and settings  
- **Customizable Decks** – Use built-in card sets or define your own domain-specific decks  
- **Domain-Specific Scenarios** – Evaluate reasoning in different industries and knowledge areas  

---

## 📦 Requirements

- **Python 3.8+**  
- **Flask**  
- **OpenAI API access**  

---

## 🛠 Installation

1. Clone the repository:
   ```bash
   git clone git@github.com:Duguce/GuessArena-Demo.git
   cd GuessArena-Demo
   ```

2. Create and activate a conda environment:
   ```
   conda create -n guessarena python=3.10
   conda activate guessarena
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Configure your API settings in `config/settings.json`

## ▶️ Usage

1. Set up your API keys in `config/models.ini` for the AI models you want to use.

2. Start the application:
   ```
   python app.py
   ```

3. Open your browser and go to `http://localhost:8888`

4. Choose between Player Mode or AI Simulation

## 📁 Project Structure

- `/config` - Configuration files and model settings
- `/data` - Leaderboard data, logs, and card decks
- `/prompts` - Prompt templates for AI models
- `/static` - Static assets (CSS, JavaScript)
- `/templates` - HTML templates

## 🔒 Security

The application includes several security features:
- Content Security Policy
- Rate limiting for API endpoints
- Path traversal prevention
- Secure file access

## 📖 Citation

```
@inproceedings{
    GuessArena,
    title = "{G}uess{A}rena: Guess Who {I} Am? A Self-Adaptive Framework for Evaluating {LLM}s in Domain-Specific Knowledge and Reasoning",
    author = "Yu, Qingchen  and
      Zheng, Zifan  and
      Chen, Ding  and
      Niu, Simin  and
      Tang, Bo  and
      Xiong, Feiyu  and
      Li, Zhiyu",
    editor = "Che, Wanxiang  and
      Nabende, Joyce  and
      Shutova, Ekaterina  and
      Pilehvar, Mohammad Taher",
    booktitle = "Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)",
    month = jul,
    year = "2025",
    address = "Vienna, Austria",
    publisher = "Association for Computational Linguistics",
    url = "https://aclanthology.org/2025.acl-long.534/",
    doi = "10.18653/v1/2025.acl-long.534",
    pages = "10897--10912",
    ISBN = "979-8-89176-251-0",
}
```