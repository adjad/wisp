# r5 picture rows: what is built, what still needs images

Built now, no model needed: `training/ask-lora-r2/builder/scen_r5_picture.py` (family `picture_turn_r5`, 40 rows,
ASK_LORA_R5=1). These go through the agent path (a request with an action word), where the picture reading is scripted text:
20 full-length document answers (every fact asked for, as a list), 14 carded proposals (7 calendar events, 7 reminders),
2 notes, 4 contrasts where the picture holds nothing to add.

Not covered: the picture-direct path, where a plain question ("When is the final exam?", "What's this?") goes to the vision
model with the image and it answers and may end with a `{"proposal": ...}` JSON. r4's gate miss (the 76-character syllabus
answer) happened there. Those rows need real rendered images, so nothing was run here.

Remaining step (on a machine that can render, no GPU needed for the images):
1. Render about 20 synthetic syllabus, schedule, receipt, form and menu pages with the existing r4 renderer (same style as
   `images/doc4_*`), each with 6-12 facts; no real names.
2. For each page write a question that asks for a whole group ("List every graded item with its date and weight") and a target
   that lists every fact in the format used in `scen_r5_picture.py` (flat list, bold labels when items have 2+ details, no
   tables or headings). Add the `proposal` JSON only when the page holds one dated event or task.
3. Keep the held-out wording out (run the builder's leak.py check) and add the rows to `vision_rows.jsonl` with ids
   `doc5_*`; rebuild `vision_stats.json`.
