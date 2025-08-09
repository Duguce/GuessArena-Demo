document.addEventListener("DOMContentLoaded", function() {
    initAIArena();
    initCardSetTypeSwitch();
    initCustomCardConfirmation();
});

// Global Variables
let maxRoundsLimit = 0;       // Default max answers from /config
let simulationConversation = [];    // Stores the full conversation log from the backend
let resultMessage = "";         // Stores the win_message from the backend
let customCardDeck = [];         // Track custom deck for consistent state

function initAIArena() {
    const industrySelectEl = document.getElementById("industry-select");
    if (industrySelectEl) {
        fetchSimulateDeckAndUpdate(industrySelectEl.value);

        // Ensure industry change works correctly with a more reliable event handler
        industrySelectEl.addEventListener("change", function() {
            fetchSimulateDeckAndUpdate(this.value);
            // Force predefined set on industry change
            document.querySelector('input[name="card_set_type"][value="predefined"]').checked = true;
            document.querySelector(".custom-options").style.display = "none";
            document.querySelector(".predefined-options").style.display = "block";
        });
    }

    document.getElementById("start-sim-btn")?.addEventListener("click", startSimulation);

    // Listen for max rounds selection change to update progress bar
    document.getElementById("max-rounds-input")?.addEventListener("change", function() {
        updateSimulateProgressBar(0, parseInt(this.value) || maxRoundsLimit);
    });

    // Fetch default config (max_answers)
    fetch("/config")
      .then(resp => resp.json())
      .then(data => {
          maxRoundsLimit = data.max_answers;
          updateSimulateProgressBar(0, maxRoundsLimit);
      })
      .catch(err => console.error("Error fetching config:", err));
}

function initCardSetTypeSwitch() {
    document.querySelectorAll('input[name="card_set_type"]').forEach(radio => {
        radio.addEventListener("change", function() {
            document.querySelector(".predefined-options").style.display = this.value === "custom" ? "none" : "block";
            document.querySelector(".custom-options").style.display = this.value === "custom" ? "flex" : "none";
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
                    updateSimulateDeckUI(customDeck);

                    // Update chosen card dropdown with custom deck options
                    updateChosenCardOptions(customDeck);

                    // Check if custom chosen card input exists and is valid
                    const customChosenCard = document.getElementById("custom-chosen-card");
                    if (customChosenCard && customChosenCard.value.trim()) {
                        const chosenCardValue = customChosenCard.value.trim();

                        // Check if the chosen card exists in the custom deck
                        if (customDeck.includes(chosenCardValue)) {
                            // Select this card in the dropdown if it exists
                            const chosenCardSelect = document.getElementById("chosen-card-select");
                            if (chosenCardSelect) {
                                for (let i = 0; i < chosenCardSelect.options.length; i++) {
                                    if (chosenCardSelect.options[i].value === chosenCardValue) {
                                        chosenCardSelect.selectedIndex = i;
                                        break;
                                    }
                                }
                            }
                        }
                    }

                    // Provide user feedback in simulation dialogue
                    const gameDialogue = document.getElementById("ai-game-dialogue");
                    if (gameDialogue) {
                        displayMessage(gameDialogue, "system-message",
                            `Custom collection confirmed with ${customDeck.length} cards. Ready for analysis.`);
                    }
                }
            }
        });
    }
}

function fetchSimulateDeckAndUpdate(industry) {
    // Add cache-busting parameter
    fetch(`/deck?industry=${encodeURIComponent(industry)}&_t=${Date.now()}`)
        .then(resp => {
            if (!resp.ok) {
                throw new Error(`Network response was not ok: ${resp.status}`);
            }
            return resp.json();
        })
        .then(data => {
            if (data && data.deck) {
                console.log(`Simulation: Received deck for industry ${industry} with ${data.deck.length} cards`);
                updateSimulateDeckUI(data.deck);
                updateChosenCardOptions(data.deck);
            } else {
                console.error("Invalid data format received:", data);
            }
        })
        .catch(err => console.error("Error fetching deck:", err));
}

function updateSimulateDeckUI(deck) {
    const deckListElem = document.getElementById("simulate-deck-list");
    if (!deckListElem) return;
    deckListElem.innerHTML = "";
    deck.forEach(card => {
        let li = document.createElement("li");
        li.textContent = card;
        deckListElem.appendChild(li);
    });
}

function updateChosenCardOptions(deck) {
    const chosenCardSelect = document.getElementById("chosen-card-select");
    if (!chosenCardSelect) return;
    chosenCardSelect.innerHTML = "";

    let defaultOption = new Option("(Auto select)", "", true);
    chosenCardSelect.appendChild(defaultOption);

    deck.forEach(card => chosenCardSelect.appendChild(new Option(card, card)));
}

function startSimulation() {
    const gameDialogue = document.getElementById("ai-game-dialogue");
    if (!gameDialogue) return;
    gameDialogue.innerHTML = '<div class="system-message">Analysis initiated. Please wait...</div>';

    const judgeModel = document.getElementById("judge-model-select")?.value || "";
    const testeeModel = document.getElementById("testee-model-select")?.value || "";
    const maxRounds = parseInt(document.getElementById("max-rounds-input")?.value) || simMaxAnswers;
    const cardSetType = document.querySelector('input[name="card_set_type"]:checked').value;
    // Add the industry value to be sent to backend
    const industry = document.getElementById("industry-select")?.value || "";

    let deck = [], chosenCard = "";

    if (cardSetType === "custom") {
        // Use the tracked customDeck if available
        deck = customDeck.length > 0 ? customDeck :
            (document.getElementById("custom-deck-input")?.value || "").split(",")
                .map(s => s.trim()).filter(Boolean);

        chosenCard = document.getElementById("custom-chosen-card")?.value.trim() || "";
    } else {
        deck = Array.from(document.querySelectorAll("#simulate-deck-list li"), li => li.textContent);
        chosenCard = document.getElementById("chosen-card-select")?.value || "";
    }

    fetch("/ai_simulation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            judge_model: judgeModel,
            testee_model: testeeModel,
            card_set_type: cardSetType,
            custom_deck: deck,
            chosen_card: chosenCard,
            max_rounds: maxRounds,
            industry: industry // Add industry to the request
        }),
    })
    .then(resp => resp.json())
    .then(data => {
        conversationLog = data.conversation || [];
        winMessage = data.win_message || "";
        renderConversationIncrementally(conversationLog, 0, maxRounds);
    })
    .catch(err => {
        console.error("Simulation Error:", err);
        gameDialogue.innerHTML += '<div class="system-message">Error occurred. Check console.</div>';
    });
}

function renderConversationIncrementally(conv, index, maxRounds) {
    const gameDialogue = document.getElementById("ai-game-dialogue");
    if (!gameDialogue || index >= conv.length) {
        if (winMessage) displayMessage(gameDialogue, "system-message", winMessage);
        return;
    }

    displayMessage(gameDialogue, conv[index].role === "user" ? "user-message" : "bot-message", conv[index].content);
    updateSimulateProgressBar(countJudgeAnswers(conv.slice(0, index + 1)), maxRounds);
    setTimeout(() => renderConversationIncrementally(conv, index + 1, maxRounds), 1000);
}

function displayMessage(container, className, text) {
    let msgDiv = document.createElement("div");
    msgDiv.className = className;
    msgDiv.textContent = text;
    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;
}

function countJudgeAnswers(conv) {
    return conv.filter(msg => msg.role === "assistant").length;
}

function updateSimulateProgressBar(current, maxVal) {
    document.getElementById("simulate-turns-text").textContent = `Responses: ${current} / ${maxVal}`;
    document.getElementById("simulate-progress-bar").style.width = `${Math.min(100, (current / maxVal) * 100)}%`;
}
