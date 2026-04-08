Task: Create a themed structured notes document from these notes.

Rules:
- Do NOT overly compress.
- Group content into themes with short meaningful titles; choose as many themes as needed for coverage (typically 4–10).
- Merge duplicates only if they express the same idea.
- Preserve reasoning, examples, numbers, names and important nuances.
- No meta commentary.

Output:

# Themes

For each theme:

## <Theme title>
Write connected paragraphs as many as you need.
Each paragraph: 2–5 complete sentences.
Maintain coherence and explicit cause→effect where present.
Avoid keyword-style writing.

Aim for ~${TARGET_TOKENS} tokens total; do not undercut heavily if important details would be lost.

Output MUST be in ${LANG}. Any non-${LANG} paragraphs are invalid; rewrite it in ${LANG}. 
If input contains mixed languages, keep original quotes, but your own text MUST be ${LANG}.
If you accidentally start writing in another language, immediately rewrite that bullet in ${LANG}.

Input:
- Approx input size: ${INPUT_TOKENS} tokens
- Target output size: ~${TARGET_TOKENS} tokens