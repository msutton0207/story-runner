# AI Basics demo

A guided, three-tab demo that shows customers how an LLM works. Backend is the Anthropic API
(default model `claude-haiku-5-5`, set `ANTHROPIC_MODEL` to change it).

## Run

    cp .env.example .env      # then put your ANTHROPIC_API_KEY in it
    node server.js            # needs Node 18+
    # open http://localhost:3100

## Tabs

1. **Prompt to answer**: system prompt (the hidden rules), your prompt, tokenization with token IDs,
   the full context payload and a window meter, and streamed inference chunk by chunk.
2. **Next-word lab**: estimates next-token probabilities by sampling the model repeatedly. Click a word to
   append it and predict again, which is the generation loop. Temperature 0 shows a single bar.
3. **Tools & agents**: a small agent loop (clock and calculator) with a trace showing the model asking
   and the app doing the work.

## Honest caveats

- The Claude API does not expose its tokenizer or token probabilities. The colored token split uses an open
  tokenizer (loaded from a CDN, with an offline approximation as fallback) and is labelled illustrative. The
  "Claude's own count" figure is the real count from the API.
- Streamed chunks are usually about one token each but are not guaranteed to be.
- The server pins the model and caps `max_tokens` at 1024 to keep demo costs low.

## New in v2

- **Ask it 5 times** (tab 1): same system prompt and question sent 5 times in parallel. Words not shared by every answer are highlighted. A temperature picker (1.0 / 0.5 / 0) shows the answers converging.
- **Forget history** (tab 1): checkbox that sends only the newest message. Say "My name is Sam", tick it, then ask "What's my name?". The context panel and token counts shrink to match.
- **Fork** (tab 2): after predicting, tick 2 to 4 candidate words and the model finishes the text from each one, side by side.
- **Compare: text only vs with tools** (tab 3): same question with and without the tool menu. "Show raw API JSON" reveals the actual tool_use blocks.
- **Grounding** (tab 4): a helpdesk bot answers an internal-policy question with and without the policy pasted into the context, with tokens-in shown for each.

## Suggested run of show (about 40 min)

1. Tab 1: system prompt and tokens. Try "FortiGate", an IP address, the same sentence in another language.
2. Tab 1: "Ask it 5 times" with "answer in 2 sentences" rules. Then temperature 0.
3. Tab 2: "A firewall is" (or "The biggest threat to a network is"). Tick the top 3, Fork.
4. Tab 3: Compare on "What time is it?" and "Big math". Turn on raw JSON.
5. Tab 1: Forget history demo.
6. Tab 4: Compare on the password question. Point at tokens-in jumping when the document is added.
7. Agent loop (tab 3, Run the agent on the default two-part question), then close.

The original page is kept as public/index.before-v2.html.

## New in v3: Risks tab (tab 5)

- **Oversharing:** a fictional document library with Everyone / HR / Finance access labels. Ask as an intern with "Search checks permissions" off and the HR document lands in the context (flagged red in the trace). Turn the check on and it's removed before ranking.
- **Hidden isn't secret:** a system prompt containing a fake discount code. "Ask 10 times and count leaks" tallies how often it leaks, with an optional "never reveal" rule to compare.
- **Denial of wallet:** cost per request, hour, day and month at an abusive request rate. Prices are placeholders; "Use my last real turn" pulls token counts from tab 1.

The previous page is kept as public/index.v2.html.


## New in v4: 201 tab (tab 6)

- **Embeddings:** turns sentences into vectors with a small open model (all-MiniLM-L6-v2) running in the browser, then shows a similarity grid. Needs internet in the browser; the first run downloads about 25 MB.
- **Keyword vs meaning search:** the Risks tab's document library searched both ways. "How do I log in from home?" shares no words with the VPN doc, so keyword search misses it.
- **Leading questions:** a neutral and a leading version of the same question, 5 runs each, graded AGREE / MIXED / DISAGREE by a second model call (LLM-as-judge). Presets: password rotation, firewalls and cloud, port 443.
- **Pass rate:** runs a prompt 10 or 20 times and checks each answer in code (sentence count, no lists, must / must not mention). The default question deliberately tempts the model to break the 2-sentence rule.

The previous page is kept as public/index.v3.html.

## Story mode (v5)

Open http://localhost:3100 (now story mode by default) for a presenter version that combines the slides and the demos in one page: 35 stops across AI 101, Risks and AI 201. Each stop with a demo has two steps: the idea first, then the live demo against the same model.

- Keys: Right arrow or Space for next, Left arrow for back, N for speaker notes, O for the list of all stops, Esc to close. Page Up / Page Down work with a presenter clicker.
- Every demo keeps its state as you move back and forth, and the next-word stop hands its text to the fork stop.
- The address bar tracks where you are (for example story.html#memory/live), so a reload or bookmark returns to the same stop.
- "Open the lab" opens the original tabbed app in a new tab; the lab has a "Story mode" link back.
- Story mode never sends a temperature setting, so it works with models that reject it.


The original tabbed lab is now at http://localhost:3100/lab.

## v6: simulations and oversharing update

- Three risk stops now have demos marked SIM in the stop list: prompt injection (a review page with a hidden note), tools turning text into actions (an inbox assistant with a pretend calendar and an optional approval step), and apps that trust the output (an image link that carries data out). Each simulation is scripted and labeled; the first two also have a button that runs the same harmless setup against the real model.
- Oversharing: a new switch adds or removes the "CONFIDENTIAL / HR ONLY" wording in the documents, and each answer gets a verdict: leaked the contents, confirmed the documents exist, held, or nothing restricted reached the model.
- The previous story page is kept as public/story.v1.html.
