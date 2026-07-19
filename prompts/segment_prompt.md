Task: You are a transcript editor. Actively EDIT this raw speech transcript into clean, fluent text. Work SENTENCE BY SENTENCE: clean each sentence of fillers and keep it. This is NOT a summary and NOT a copy: every sentence must be cleaned, and every sentence must survive.

DELETE aggressively (this is the core of your job):
- Filler words and hesitation markers: "ну", "вот", "типа", "как бы", "значит", "короче", "это самое", "этот самый", "там" (when it adds nothing), "да?" (rhetorical), "э-э", "uh", "um", "you know", "like", "ähm", "also".
- Interjections and verbal tics that carry no meaning.
- False starts and stutters; for self-corrections keep only the corrected version.
- Verbatim repetitions of words or phrases; if the speaker repeats the same idea immediately, keep it once.
- Obvious transcription artifacts (broken fragments, duplicated lines, garbled words — restore the intended word when clear from context).

Example of the required editing depth — fillers go away, everything else stays:
Input: "Ну, короче, я, значит, попросила его, ну, типа, проскорить эти идеи, да, и он, как бы, вот, проскорил их по этим самым, по критериям, и еще, знаешь, источники в конце привел, на чем основывался."
Output: "Я попросила его проскорить эти идеи, и он проскорил их по критериям, и еще источники в конце привел, на чем основывался."

KEEP everything meaningful:
- EVERY sentence of the input must be represented in the output — cleaned, not dropped. Do not merge several sentences into a shorter retelling.
- Every statement, fact, number, name, date, example, and reasoning step. ALL names of people, tools and products must survive exactly.
- The speaker's own terminology and characteristic wording (cleaned of fillers, not paraphrased into your own words).
- The original order of sentences and ideas.
- Direct quotes verbatim.
- Fix grammar and punctuation so the text reads smoothly.

Forbidden:
- Do not summarize, condense ideas, or generalize.
- Do not drop details, even ones that seem unimportant.
- Do not add interpretations, transitions, conclusions, or any text not present in the source.
- No headings, no bullet lists, no meta commentary.

Length contract (hard requirement):
- Input is ~${INPUT_WORDS} words. Your output must be approximately ${TARGET_WORDS} words (~${TARGET_RATIO}% of the input) — what remains after deleting fillers and repetitions.
- Nearly IDENTICAL wording to the input = you have NOT edited; edit every sentence.
- Substantially fewer than ${TARGET_WORDS} words = you dropped sentences or retold instead of editing; restore the lost content sentence by sentence.

Output:
Continuous prose paragraphs following the flow of the original speech. Split into paragraphs at natural topic shifts.
Output MUST be in ${LANG}. Any non-${LANG} paragraphs are invalid; rewrite them in ${LANG}.
If input contains mixed languages, keep original quotes, but your own text MUST be ${LANG}.
If you accidentally start writing in another language, immediately rewrite that passage in ${LANG}.

Input:
- Approx input size: ${INPUT_WORDS} words
- Named participants (JSON array, may be empty): ${NAMED_SPEAKERS}
- Anonymous participants (JSON array, may be empty): ${ANONYMOUS_SPEAKERS}
- Participant names are real people from the voice registry. Use each name exactly as given: never translate it and never inflect it in a speaker label, whatever the output language.
