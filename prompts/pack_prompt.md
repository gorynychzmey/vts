Task: Pack these notes so they fit into a smaller context window.

Input:
- Approx input size: ${INPUT_TOKENS} tokens
- Target output size: ~${TARGET_TOKENS} tokens

Rules:
- This is NOT summarization. Do NOT aggressively compress.
- Remove true duplicates and near-duplicates.
- Remove speech noise / repeated filler.
- Preserve all non-trivial points, reasoning chains, examples, numbers, names.
- Keep clear logical flow.
- No meta commentary.

Output:
Write short connected paragraphs (2–4 sentences each), as many as needed.
Aim for ~${TARGET_TOKENS} tokens; prefer dropping duplicates over dropping unique information.
No headings.
Output MUST be in ${LANG}. Any non-${LANG} paragraphs are invalid; rewrite it in ${LANG}. 
If input contains mixed languages, keep original quotes, but your own text MUST be ${LANG}.
If you accidentally start writing in another language, immediately rewrite that bullet in ${LANG}.
