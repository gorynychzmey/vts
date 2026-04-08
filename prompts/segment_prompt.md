Task: Write a concise but coherent note-style synopsis of this transcript segment.

Faithfulness rules:
- Every statement must be directly supported by the source text.
- Treat the transcript as a technical source that must not be semantically altered.
- Do not introduce interpretations that are not clearly stated.
- Do not replace precise wording with a looser paraphrase.
- If the original wording is already clear and accurate, keep it close to the source.
- Prefer explicit nouns instead of vague pronouns.
- If removing words changes the meaning, keep the original phrasing.

General rules:
- Remove filler speech and repetitions only.
- Preserve reasoning chains, examples, numbers, and names.
- Keep the original order of ideas.
- Avoid stylistic improvements that change meaning.
- No meta commentary.

Output:
Write short connected paragraphs (2–4 sentences each), as many as needed to cover all non-trivial points.
Aim for ~${TARGET_TOKENS} tokens; do not undercut heavily if important details would be lost.
No headings.
Output MUST be in ${LANG}. Any non-${LANG} paragraphs are invalid; rewrite it in ${LANG}. 
If input contains mixed languages, keep original quotes, but your own text MUST be ${LANG}.
If you accidentally start writing in another language, immediately rewrite that bullet in ${LANG}.

Input:
- Approx input size: ${INPUT_TOKENS} tokens
- Target output size: ~${TARGET_TOKENS} tokens