# Make your own trivia pack

The shipped half-time pack is Irish/UK-centric. To localise it, put a
`trivia_custom.json` in the app's data directory — with the default
`docker-compose.yml` that's `./data/trivia_custom.json`, right beside `quiz.db`.
It's a flat JSON list of two kinds of item:

    [
      {"kind": "fact", "text": "Johnny Cash proposed to June Carter on stage in London, Ontario."},
      {"kind": "tf",   "text": "Gordon Lightfoot wrote 'Early Morning Rain'.", "answer": 1},
      {"kind": "tf",   "text": "Céline Dion is from Vancouver.", "answer": 0}
    ]

- `fact` items are read aloud by a player at half time — write them as spoken
  sentences, and only include things you'd defend at your own kitchen table.
- `tf` items need `"answer": 1` (true) or `0` (false). Keep a healthy share of
  falses (the shipped pack runs ~60/40) or the table learns to always guess true.
- The pack seeds automatically at the next game start (or `POST /api/trivia/topup`).
  Malformed items are skipped with a log warning, never fatally. Duplicate texts
  are ignored, so you can keep growing the file and re-seeding.
- Set `TRIVIA_BUILTIN_PACK=false` in `.env` **before your first game** to skip
  the shipped pack entirely — items already seeded stay in the bank (pruning
  after the fact is a `DELETE FROM trivia WHERE source='seed'` in `data/quiz.db`).
- An LLM drafts a regional pack in minutes. Copy this prompt, swap the region,
  and paste the output into `data/trivia_custom.json` — but **fact-check what it
  writes before your family reads it out with confidence.** LLMs state wrong
  "facts" fluently.

      Write me a music trivia pack for a family quiz night, as a single JSON
      list and nothing else. Two kinds of item:
        {"kind": "fact", "text": "..."}                 — a fun music fact one
          player reads aloud to the table
        {"kind": "tf", "text": "...", "answer": 1 or 0} — a true/false question
          (1 = true, 0 = false)

      Produce 60 facts and 80 true/false questions about popular music from
      <YOUR REGION/COUNTRY>, plus internationally famous acts as heard from
      there. Rules:
      - Only well-established, easily verifiable claims — no obscure trivia,
        no disputed stories. If a story is folklore, start it with "Legend has
        it" or "As the story goes".
      - Facts must read naturally when spoken aloud, one or two sentences.
      - False T/F statements must be plainly false, not technicalities.
      - Make roughly 40% of the T/F items false, and don't cluster them.
      - Family-friendly; span the 1960s to today; vary the artists — no more
        than three items about any one act.
      - Output raw JSON only: no markdown fences, no commentary.
