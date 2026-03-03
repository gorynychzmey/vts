Task: Write a concise but coherent note-style synopsis of this transcript segment.

Input:
- Approx input size: ${INPUT_TOKENS} tokens
- Target output size: ~${TARGET_TOKENS} tokens

Rules:
- Not a short abstract. Not verbatim.
- Preserve reasoning, cause→effect links, examples, numbers, names.
- Remove filler and repetitions only.
- Keep original order of ideas.
- No meta commentary.

Output:
Write short connected paragraphs (2–4 sentences each), as many as needed to cover all non-trivial points.
Aim for ~${TARGET_TOKENS} tokens; do not undercut heavily if important details would be lost.
No headings.
Output MUST be in ${LANG}. Any non-${LANG} paragraphs are invalid; rewrite it in ${LANG}. 
If input contains mixed languages, keep original quotes, but your own text MUST be ${LANG}.
If you accidentally start writing in another language, immediately rewrite that bullet in ${LANG}.
