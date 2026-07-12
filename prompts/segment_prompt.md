Task: Rewrite this transcript segment as clean, fluent text. This is NOT a summary. The output must contain the same speech as the input — only smoothed.

Remove ONLY:
- Filler words and hesitation markers (e.g. "uh", "um", "you know", "like", "ну", "вот", "как бы", "значит", "э-э", "ähm", "also").
- Interjections and verbal tics that carry no meaning.
- False starts and stutters; for self-corrections keep only the corrected version.
- Verbatim repetitions of the same word or phrase; if the speaker repeats the same idea immediately, keep it once.
- Obvious transcription artifacts (broken fragments, duplicated lines).

Keep EVERYTHING else:
- Every statement, fact, number, name, date, example, and reasoning step.
- The speaker's own wording and terminology. Do not paraphrase, do not replace precise wording with a looser one.
- The original order of sentences and ideas.
- Direct quotes verbatim.
- Fix grammar and punctuation only minimally, so the text reads smoothly.

Forbidden:
- Do not summarize, condense, shorten, or generalize.
- Do not drop details, even ones that seem unimportant.
- Do not add interpretations, transitions, conclusions, or any text not present in the source.
- No headings, no bullet lists, no meta commentary.

Output:
Continuous prose paragraphs following the flow of the original speech. Split into paragraphs at natural topic shifts.
The output must be nearly as long as the input (input minus fillers and repetitions). If your output is much shorter than the input, you have summarized — that is an error; rewrite preserving all content.
Output MUST be in ${LANG}. Any non-${LANG} paragraphs are invalid; rewrite them in ${LANG}.
If input contains mixed languages, keep original quotes, but your own text MUST be ${LANG}.
If you accidentally start writing in another language, immediately rewrite that passage in ${LANG}.

Input:
- Approx input size: ${INPUT_WORDS} words
