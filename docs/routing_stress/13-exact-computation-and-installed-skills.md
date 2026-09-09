# Review only — no tests run

## 13. Exact computation and installed skills

Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

### WRS-0601 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: draw a square using my ASCII shape tool; then generate a 20-character alphanumeric test password without symbols; then list my installed skills and whether they are enabled; then flip a coin using real randomness; then use my installed stick-figure tool to draw a person in ASCII.

**Required tools:** `ascii_art_generator`, `generate_password`, `wisp_skills`, `random_pick`, `human_shape`.
**Ordering constraints:** `ascii_art_generator` before `generate_password`; `generate_password` before `wisp_skills`; `wisp_skills` before `random_pick`; `random_pick` before `human_shape`.
**Checks:** shape square length 20; symbols false inventory only; no skill execution no options; Heads or Tails no arguments; fixed figure Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Four callable skill fixtures plus instruction-only skills
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Installed skill tool; no image-generation API

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0602 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: draw a square using my ASCII shape tool; then list my installed skills and whether they are enabled; then use my installed stick-figure tool to draw a person in ASCII; then use my vowel counter to count the vowels in banana; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays.

**Required tools:** `ascii_art_generator`, `wisp_skills`, `human_shape`, `count_vowels`, `business_days_between`.
**Ordering constraints:** `ascii_art_generator` before `wisp_skills`; `wisp_skills` before `human_shape`; `human_shape` before `count_vowels`; `count_vowels` before `business_days_between`.
**Checks:** shape square inventory only; no skill execution no arguments; fixed figure word banana; expected 3 start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Four callable skill fixtures plus instruction-only skills
- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0603 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then flip a coin using real randomness; then draw a square using my ASCII shape tool; then use my vowel counter to count the vowels in banana; then generate a 20-character alphanumeric test password without symbols.

**Required tools:** `business_days_between`, `random_pick`, `ascii_art_generator`, `count_vowels`, `generate_password`.
**Ordering constraints:** `business_days_between` before `random_pick`; `random_pick` before `ascii_art_generator`; `ascii_art_generator` before `count_vowels`; `count_vowels` before `generate_password`.
**Checks:** start 2026-09-01; end 2026-09-10; both endpoints inclusive no options; Heads or Tails shape square word banana; expected 3 length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill; expected 8 weekdays
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill counts a/e/i/o/u only
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0604 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: calculate 18 percent of 64.50 exactly; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0604/output/status.txt; then use my vowel counter to count the vowels in banana; then load the full instructions for the installed interview-me skill, without starting the interview yet; then list my installed skills and whether they are enabled.

**Required tools:** `calculate`, `write_file`, `count_vowels`, `use_skill`, `wisp_skills`.
**Ordering constraints:** `calculate` before `write_file`; `write_file` before `count_vowels`; `count_vowels` before `use_skill`; `use_skill` before `wisp_skills`.
**Checks:** expression equivalent to 0.18*64.50; result 11.61 exact path/content; text file; not Notes word banana; expected 3 name interview-me; load only if not already injected inventory only; no skill execution Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- No external data needed
- Synthetic output directory writable; file absent
- Installed skill counts a/e/i/o/u only
- Skill exists but its body is not yet in this synthetic turn's context
- Four callable skill fixtures plus instruction-only skills

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0605 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: generate a 20-character alphanumeric test password without symbols; then draw a square using my ASCII shape tool; then use my vowel counter to count the vowels in banana; then flip a coin using real randomness; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays.

**Required tools:** `generate_password`, `ascii_art_generator`, `count_vowels`, `random_pick`, `business_days_between`.
**Ordering constraints:** `generate_password` before `ascii_art_generator`; `ascii_art_generator` before `count_vowels`; `count_vowels` before `random_pick`; `random_pick` before `business_days_between`.
**Checks:** length 20; symbols false shape square word banana; expected 3 no options; Heads or Tails start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill counts a/e/i/o/u only
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0606 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: generate a 20-character alphanumeric test password without symbols; then load the full instructions for the installed interview-me skill, without starting the interview yet; then use my installed stick-figure tool to draw a person in ASCII; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then use my vowel counter to count the vowels in banana.

**Required tools:** `generate_password`, `use_skill`, `human_shape`, `business_days_between`, `count_vowels`.
**Ordering constraints:** `generate_password` before `use_skill`; `use_skill` before `human_shape`; `human_shape` before `business_days_between`; `business_days_between` before `count_vowels`.
**Checks:** length 20; symbols false name interview-me; load only if not already injected no arguments; fixed figure start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill tool; no image-generation API
- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0607 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: use my installed stick-figure tool to draw a person in ASCII; then list my installed skills and whether they are enabled; then use my vowel counter to count the vowels in banana; then generate a 20-character alphanumeric test password without symbols; then pick two different choices randomly from tea, coffee, and water.

**Required tools:** `human_shape`, `wisp_skills`, `count_vowels`, `generate_password`, `random_pick`.
**Ordering constraints:** `human_shape` before `wisp_skills`; `wisp_skills` before `count_vowels`; `count_vowels` before `generate_password`; `generate_password` before `random_pick`.
**Checks:** no arguments; fixed figure inventory only; no skill execution word banana; expected 3 length 20; symbols false options exact; count 2; no repeated choice Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Four callable skill fixtures plus instruction-only skills
- Installed skill counts a/e/i/o/u only
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0608 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: pick two different choices randomly from tea, coffee, and water; then load the full instructions for the installed interview-me skill, without starting the interview yet; then list my installed skills and whether they are enabled; then draw a circle using my ASCII shape tool; then use my installed stick-figure tool to draw a person in ASCII.

**Required tools:** `random_pick`, `use_skill`, `wisp_skills`, `ascii_art_generator`, `human_shape`.
**Ordering constraints:** `random_pick` before `use_skill`; `use_skill` before `wisp_skills`; `wisp_skills` before `ascii_art_generator`; `ascii_art_generator` before `human_shape`.
**Checks:** options exact; count 2; no repeated choice name interview-me; load only if not already injected inventory only; no skill execution shape circle no arguments; fixed figure Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Skill exists but its body is not yet in this synthetic turn's context
- Four callable skill fixtures plus instruction-only skills
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Installed skill tool; no image-generation API

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0609 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: load the full instructions for the installed interview-me skill, without starting the interview yet; then use my installed stick-figure tool to draw a person in ASCII; then use my vowel counter to count the vowels in banana; then draw a triangle using the installed ASCII shape generator; then generate a 24-character random test password including symbols.

**Required tools:** `use_skill`, `human_shape`, `count_vowels`, `ascii_art_generator`, `generate_password`.
**Ordering constraints:** `use_skill` before `human_shape`; `human_shape` before `count_vowels`; `count_vowels` before `ascii_art_generator`; `ascii_art_generator` before `generate_password`.
**Checks:** name interview-me; load only if not already injected no arguments; fixed figure word banana; expected 3 shape triangle; not raster image length 24; symbols true; do not save it Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only
- Installed shape-generator script available through intercepted skill call
- Synthetic secret output; redact result in reports

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0610 · Explicit sequence

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Please do these in this order: load the full instructions for the installed interview-me skill, without starting the interview yet; then use my installed stick-figure tool to draw a person in ASCII; then pick two different choices randomly from tea, coffee, and water; then use my vowel counter to count the vowels in banana; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays.

**Required tools:** `use_skill`, `human_shape`, `random_pick`, `count_vowels`, `business_days_between`.
**Ordering constraints:** `use_skill` before `human_shape`; `human_shape` before `random_pick`; `random_pick` before `count_vowels`; `count_vowels` before `business_days_between`.
**Checks:** name interview-me; load only if not already injected no arguments; fixed figure options exact; count 2; no repeated choice word banana; expected 3 start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill tool; no image-generation API
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Installed skill counts a/e/i/o/u only
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0611 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Draw a circle using my ASCII shape tool. Pick two different choices randomly from tea, coffee, and water. Use my installed stick-figure tool to draw a person in ASCII. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Generate a 20-character alphanumeric test password without symbols. Keep the results separate so I can tell what came from where.

**Required tools:** `ascii_art_generator`, `random_pick`, `human_shape`, `business_days_between`, `generate_password`.
**Checks:** shape circle options exact; count 2; no repeated choice no arguments; fixed figure start 2026-09-01; end 2026-09-10; both endpoints inclusive length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Installed skill tool; no image-generation API
- Installed skill; expected 8 weekdays
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0612 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Use my vowel counter to count the vowels in banana. Load the full instructions for the installed interview-me skill, without starting the interview yet. List my installed skills and whether they are enabled. Randomly pick one of tea, coffee, and water. Keep the results separate so I can tell what came from where.

**Required tools:** `business_days_between`, `count_vowels`, `use_skill`, `wisp_skills`, `random_pick`.
**Checks:** start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 name interview-me; load only if not already injected inventory only; no skill execution options exactly tea, coffee, water; count 1 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only
- Skill exists but its body is not yet in this synthetic turn's context
- Four callable skill fixtures plus instruction-only skills
- OS randomness intercepted; result one offered option

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0613 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Use my vowel counter to count the vowels in banana. Load the full instructions for the installed interview-me skill, without starting the interview yet. Use my installed stick-figure tool to draw a person in ASCII. List my installed skills and whether they are enabled. Generate a 20-character alphanumeric test password without symbols. Keep the results separate so I can tell what came from where.

**Required tools:** `count_vowels`, `use_skill`, `human_shape`, `wisp_skills`, `generate_password`.
**Checks:** word banana; expected 3 name interview-me; load only if not already injected no arguments; fixed figure inventory only; no skill execution length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill counts a/e/i/o/u only
- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill tool; no image-generation API
- Four callable skill fixtures plus instruction-only skills
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0614 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Generate a 20-character alphanumeric test password without symbols. Draw a square using my ASCII shape tool. Use my installed stick-figure tool to draw a person in ASCII. Flip a coin using real randomness. Load the full instructions for the installed interview-me skill, without starting the interview yet. Keep the results separate so I can tell what came from where.

**Required tools:** `generate_password`, `ascii_art_generator`, `human_shape`, `random_pick`, `use_skill`.
**Checks:** length 20; symbols false shape square no arguments; fixed figure no options; Heads or Tails name interview-me; load only if not already injected Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill tool; no image-generation API
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Skill exists but its body is not yet in this synthetic turn's context

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0615 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Generate a 20-character alphanumeric test password without symbols. Use my vowel counter to count the vowels in banana. Draw a circle using my ASCII shape tool. Pick two different choices randomly from tea, coffee, and water. List my installed skills and whether they are enabled. Keep the results separate so I can tell what came from where.

**Required tools:** `generate_password`, `count_vowels`, `ascii_art_generator`, `random_pick`, `wisp_skills`.
**Checks:** length 20; symbols false word banana; expected 3 shape circle options exact; count 2; no repeated choice inventory only; no skill execution Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed skill counts a/e/i/o/u only
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Four callable skill fixtures plus instruction-only skills

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0616 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Generate a 20-character alphanumeric test password without symbols. Load the full instructions for the installed interview-me skill, without starting the interview yet. Use my installed stick-figure tool to draw a person in ASCII. Use my vowel counter to count the vowels in banana. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Keep the results separate so I can tell what came from where.

**Required tools:** `generate_password`, `use_skill`, `human_shape`, `count_vowels`, `business_days_between`.
**Checks:** length 20; symbols false name interview-me; load only if not already injected no arguments; fixed figure word banana; expected 3 start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0617 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. Pick two different choices randomly from tea, coffee, and water. Draw a circle using my ASCII shape tool. Use my vowel counter to count the vowels in banana. Generate a 20-character alphanumeric test password without symbols. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Keep the results separate so I can tell what came from where.

**Required tools:** `random_pick`, `ascii_art_generator`, `count_vowels`, `generate_password`, `business_days_between`.
**Checks:** options exact; count 2; no repeated choice shape circle word banana; expected 3 length 20; symbols false start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Installed skill counts a/e/i/o/u only
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0618 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. List my installed skills and whether they are enabled. Use my installed stick-figure tool to draw a person in ASCII. Use my vowel counter to count the vowels in banana. Draw a circle using my ASCII shape tool. Generate a 20-character alphanumeric test password without symbols. Keep the results separate so I can tell what came from where.

**Required tools:** `wisp_skills`, `human_shape`, `count_vowels`, `ascii_art_generator`, `generate_password`.
**Checks:** inventory only; no skill execution no arguments; fixed figure word banana; expected 3 shape circle length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Four callable skill fixtures plus instruction-only skills
- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0619 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. List my installed skills and whether they are enabled. Flip a coin using real randomness. Use my installed stick-figure tool to draw a person in ASCII. Draw a square using my ASCII shape tool. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Keep the results separate so I can tell what came from where.

**Required tools:** `wisp_skills`, `random_pick`, `human_shape`, `ascii_art_generator`, `business_days_between`.
**Checks:** inventory only; no skill execution no options; Heads or Tails no arguments; fixed figure shape square start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Four callable skill fixtures plus instruction-only skills
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Installed skill tool; no image-generation API
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0620 · Natural compound request

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

I have a few things to finish. List my installed skills and whether they are enabled. Load the full instructions for the installed interview-me skill, without starting the interview yet. Use my installed stick-figure tool to draw a person in ASCII. Generate a 20-character alphanumeric test password without symbols. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Keep the results separate so I can tell what came from where.

**Required tools:** `wisp_skills`, `use_skill`, `human_shape`, `generate_password`, `business_days_between`.
**Checks:** inventory only; no skill execution name interview-me; load only if not already injected no arguments; fixed figure length 20; symbols false start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Four callable skill fixtures plus instruction-only skills
- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill tool; no image-generation API
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0621 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: draw a circle using my ASCII shape tool; then pick two different choices randomly from tea, coffee, and water; then use my vowel counter to count the vowels in banana; then use my installed stick-figure tool to draw a person in ASCII; then load the full instructions for the installed interview-me skill, without starting the interview yet. Leave everything else unchanged.

**Required tools:** `ascii_art_generator`, `random_pick`, `count_vowels`, `human_shape`, `use_skill`.
**Ordering constraints:** `ascii_art_generator` before `random_pick`; `random_pick` before `count_vowels`; `count_vowels` before `human_shape`; `human_shape` before `use_skill`.
**Checks:** shape circle options exact; count 2; no repeated choice word banana; expected 3 no arguments; fixed figure name interview-me; load only if not already injected Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Installed skill counts a/e/i/o/u only
- Installed skill tool; no image-generation API
- Skill exists but its body is not yet in this synthetic turn's context

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0622 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then load the full instructions for the installed interview-me skill, without starting the interview yet; then draw a circle using my ASCII shape tool; then list my installed skills and whether they are enabled; then pick two different choices randomly from tea, coffee, and water. Leave everything else unchanged.

**Required tools:** `business_days_between`, `use_skill`, `ascii_art_generator`, `wisp_skills`, `random_pick`.
**Ordering constraints:** `business_days_between` before `use_skill`; `use_skill` before `ascii_art_generator`; `ascii_art_generator` before `wisp_skills`; `wisp_skills` before `random_pick`.
**Checks:** start 2026-09-01; end 2026-09-10; both endpoints inclusive name interview-me; load only if not already injected shape circle inventory only; no skill execution options exact; count 2; no repeated choice Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill; expected 8 weekdays
- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Four callable skill fixtures plus instruction-only skills
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0623 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: generate a 20-character alphanumeric test password without symbols; then load the full instructions for the installed interview-me skill, without starting the interview yet; then draw a square using my ASCII shape tool; then list my installed skills and whether they are enabled; then flip a coin using real randomness. Leave everything else unchanged.

**Required tools:** `generate_password`, `use_skill`, `ascii_art_generator`, `wisp_skills`, `random_pick`.
**Ordering constraints:** `generate_password` before `use_skill`; `use_skill` before `ascii_art_generator`; `ascii_art_generator` before `wisp_skills`; `wisp_skills` before `random_pick`.
**Checks:** length 20; symbols false name interview-me; load only if not already injected shape square inventory only; no skill execution no options; Heads or Tails Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Four callable skill fixtures plus instruction-only skills
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0624 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: use my installed stick-figure tool to draw a person in ASCII; then use my vowel counter to count the vowels in banana; then draw a circle using my ASCII shape tool; then generate a 20-character alphanumeric test password without symbols; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Leave everything else unchanged.

**Required tools:** `human_shape`, `count_vowels`, `ascii_art_generator`, `generate_password`, `business_days_between`.
**Ordering constraints:** `human_shape` before `count_vowels`; `count_vowels` before `ascii_art_generator`; `ascii_art_generator` before `generate_password`; `generate_password` before `business_days_between`.
**Checks:** no arguments; fixed figure word banana; expected 3 shape circle length 20; symbols false start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0625 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: use my installed stick-figure tool to draw a person in ASCII; then flip a coin using real randomness; then use my vowel counter to count the vowels in banana; then list my installed skills and whether they are enabled; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Leave everything else unchanged.

**Required tools:** `human_shape`, `random_pick`, `count_vowels`, `wisp_skills`, `business_days_between`.
**Ordering constraints:** `human_shape` before `random_pick`; `random_pick` before `count_vowels`; `count_vowels` before `wisp_skills`; `wisp_skills` before `business_days_between`.
**Checks:** no arguments; fixed figure no options; Heads or Tails word banana; expected 3 inventory only; no skill execution start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Installed skill counts a/e/i/o/u only
- Four callable skill fixtures plus instruction-only skills
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0626 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: use my installed stick-figure tool to draw a person in ASCII; then list my installed skills and whether they are enabled; then calculate 18 percent of 64.50 exactly; then pick two different choices randomly from tea, coffee, and water; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Leave everything else unchanged.

**Required tools:** `human_shape`, `wisp_skills`, `calculate`, `random_pick`, `business_days_between`.
**Ordering constraints:** `human_shape` before `wisp_skills`; `wisp_skills` before `calculate`; `calculate` before `random_pick`; `random_pick` before `business_days_between`.
**Checks:** no arguments; fixed figure inventory only; no skill execution expression equivalent to 0.18*64.50; result 11.61 options exact; count 2; no repeated choice start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Four callable skill fixtures plus instruction-only skills
- No external data needed
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0627 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: load the full instructions for the installed interview-me skill, without starting the interview yet; then draw a square using my ASCII shape tool; then use my installed stick-figure tool to draw a person in ASCII; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then use my vowel counter to count the vowels in banana. Leave everything else unchanged.

**Required tools:** `use_skill`, `ascii_art_generator`, `human_shape`, `business_days_between`, `count_vowels`.
**Ordering constraints:** `use_skill` before `ascii_art_generator`; `ascii_art_generator` before `human_shape`; `human_shape` before `business_days_between`; `business_days_between` before `count_vowels`.
**Checks:** name interview-me; load only if not already injected shape square no arguments; fixed figure start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill tool; no image-generation API
- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0628 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: load the full instructions for the installed interview-me skill, without starting the interview yet; then flip a coin using real randomness; then list my installed skills and whether they are enabled; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then use my vowel counter to count the vowels in banana. Leave everything else unchanged.

**Required tools:** `use_skill`, `random_pick`, `wisp_skills`, `business_days_between`, `count_vowels`.
**Ordering constraints:** `use_skill` before `random_pick`; `random_pick` before `wisp_skills`; `wisp_skills` before `business_days_between`; `business_days_between` before `count_vowels`.
**Checks:** name interview-me; load only if not already injected no options; Heads or Tails inventory only; no skill execution start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Four callable skill fixtures plus instruction-only skills
- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0629 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: load the full instructions for the installed interview-me skill, without starting the interview yet; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0629/output/status.txt; then generate a 20-character alphanumeric test password without symbols; then flip a coin using real randomness; then convert 68 degrees Fahrenheit to Celsius. Leave everything else unchanged.

**Required tools:** `use_skill`, `write_file`, `generate_password`, `random_pick`, `convert_units`.
**Ordering constraints:** `use_skill` before `write_file`; `write_file` before `generate_password`; `generate_password` before `random_pick`; `random_pick` before `convert_units`.
**Checks:** name interview-me; load only if not already injected exact path/content; text file; not Notes length 20; symbols false no options; Heads or Tails value 68; F to C; expected 20 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- Synthetic output directory writable; file absent
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0630 · Scoped execution

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

For these tasks, use only the named sources and targets: list my installed skills and whether they are enabled; then generate a 24-character random test password including symbols; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then use my installed stick-figure tool to draw a person in ASCII; then randomly pick one of tea, coffee, and water. Leave everything else unchanged.

**Required tools:** `wisp_skills`, `generate_password`, `business_days_between`, `human_shape`, `random_pick`.
**Ordering constraints:** `wisp_skills` before `generate_password`; `generate_password` before `business_days_between`; `business_days_between` before `human_shape`; `human_shape` before `random_pick`.
**Checks:** inventory only; no skill execution length 24; symbols true; do not save it start 2026-09-01; end 2026-09-10; both endpoints inclusive no arguments; fixed figure options exactly tea, coffee, water; count 1 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Four callable skill fixtures plus instruction-only skills
- Synthetic secret output; redact result in reports
- Installed skill; expected 8 weekdays
- Installed skill tool; no image-generation API
- OS randomness intercepted; result one offered option

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0631 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Use my vowel counter to count the vowels in banana. List my installed skills and whether they are enabled. Flip a coin using real randomness. Generate a 20-character alphanumeric test password without symbols. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `business_days_between`, `count_vowels`, `wisp_skills`, `random_pick`, `generate_password`.
**Checks:** start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 inventory only; no skill execution no options; Heads or Tails length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only
- Four callable skill fixtures plus instruction-only skills
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0632 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Load the full instructions for the installed interview-me skill, without starting the interview yet. List my installed skills and whether they are enabled. Generate a 20-character alphanumeric test password without symbols. Draw a square using my ASCII shape tool. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `business_days_between`, `use_skill`, `wisp_skills`, `generate_password`, `ascii_art_generator`.
**Checks:** start 2026-09-01; end 2026-09-10; both endpoints inclusive name interview-me; load only if not already injected inventory only; no skill execution length 20; symbols false shape square Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill; expected 8 weekdays
- Skill exists but its body is not yet in this synthetic turn's context
- Four callable skill fixtures plus instruction-only skills
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0633 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. List my installed skills and whether they are enabled. Load the full instructions for the installed interview-me skill, without starting the interview yet. Draw a square using my ASCII shape tool. Generate a 20-character alphanumeric test password without symbols. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `business_days_between`, `wisp_skills`, `use_skill`, `ascii_art_generator`, `generate_password`.
**Checks:** start 2026-09-01; end 2026-09-10; both endpoints inclusive inventory only; no skill execution name interview-me; load only if not already injected shape square length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill; expected 8 weekdays
- Four callable skill fixtures plus instruction-only skills
- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0634 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Convert 100 US dollars to euros using the latest available rate and state its date. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Calculate 18 percent of 64.50 exactly. Draw a square using my ASCII shape tool. Use my vowel counter to count the vowels in banana. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `convert_currency`, `business_days_between`, `calculate`, `ascii_art_generator`, `count_vowels`.
**Checks:** amount 100; USD to EUR; retrieved rate date start 2026-09-01; end 2026-09-10; both endpoints inclusive expression equivalent to 0.18*64.50; result 11.61 shape square word banana; expected 3 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Installed skill; expected 8 weekdays
- No external data needed
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill counts a/e/i/o/u only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0635 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Convert 68 degrees Fahrenheit to Celsius. Convert 100 US dollars to euros using the latest available rate and state its date. Draw a circle using my ASCII shape tool. Load the full instructions for the installed interview-me skill, without starting the interview yet. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0635/output/status.txt. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `convert_units`, `convert_currency`, `ascii_art_generator`, `use_skill`, `write_file`.
**Checks:** value 68; F to C; expected 20 amount 100; USD to EUR; retrieved rate date shape circle name interview-me; load only if not already injected exact path/content; text file; not Notes Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Skill exists but its body is not yet in this synthetic turn's context
- Synthetic output directory writable; file absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0636 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Use my installed stick-figure tool to draw a person in ASCII. Load the full instructions for the installed interview-me skill, without starting the interview yet. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Use my vowel counter to count the vowels in banana. Generate a 20-character alphanumeric test password without symbols. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `human_shape`, `use_skill`, `business_days_between`, `count_vowels`, `generate_password`.
**Checks:** no arguments; fixed figure name interview-me; load only if not already injected start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0637 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Flip a coin using real randomness. Generate a 20-character alphanumeric test password without symbols. Use my installed stick-figure tool to draw a person in ASCII. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. List my installed skills and whether they are enabled. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `random_pick`, `generate_password`, `human_shape`, `business_days_between`, `wisp_skills`.
**Checks:** no options; Heads or Tails length 20; symbols false no arguments; fixed figure start 2026-09-01; end 2026-09-10; both endpoints inclusive inventory only; no skill execution Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Installed skill tool; no image-generation API
- Installed skill; expected 8 weekdays
- Four callable skill fixtures plus instruction-only skills

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0638 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Load the full instructions for the installed interview-me skill, without starting the interview yet. Draw a triangle using the installed ASCII shape generator. Use my vowel counter to count the vowels in banana. Randomly pick one of tea, coffee, and water. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `use_skill`, `ascii_art_generator`, `count_vowels`, `random_pick`, `business_days_between`.
**Checks:** name interview-me; load only if not already injected shape triangle; not raster image word banana; expected 3 options exactly tea, coffee, water; count 1 start 2026-09-01; end 2026-09-10; both endpoints inclusive Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call
- Installed skill counts a/e/i/o/u only
- OS randomness intercepted; result one offered option
- Installed skill; expected 8 weekdays

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0639 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Load the full instructions for the installed interview-me skill, without starting the interview yet. Pick two different choices randomly from tea, coffee, and water. List my installed skills and whether they are enabled. Use my installed stick-figure tool to draw a person in ASCII. Use my vowel counter to count the vowels in banana. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `use_skill`, `random_pick`, `wisp_skills`, `human_shape`, `count_vowels`.
**Checks:** name interview-me; load only if not already injected options exact; count 2; no repeated choice inventory only; no skill execution no arguments; fixed figure word banana; expected 3 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Four callable skill fixtures plus instruction-only skills
- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0640 · Late constraints

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

List my installed skills and whether they are enabled. Draw a circle using my ASCII shape tool. Use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays. Load the full instructions for the installed interview-me skill, without starting the interview yet. Use my vowel counter to count the vowels in banana. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `wisp_skills`, `ascii_art_generator`, `business_days_between`, `use_skill`, `count_vowels`.
**Checks:** inventory only; no skill execution shape circle start 2026-09-01; end 2026-09-10; both endpoints inclusive name interview-me; load only if not already injected word banana; expected 3 Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Four callable skill fixtures plus instruction-only skills
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Installed skill; expected 8 weekdays
- Skill exists but its body is not yet in this synthetic turn's context
- Installed skill counts a/e/i/o/u only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0641 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you draw a circle using my ASCII shape tool; then use my vowel counter to count the vowels in banana; then convert 68 degrees Fahrenheit to Celsius; then convert 100 US dollars to euros using the latest available rate and state its date; then generate a 20-character alphanumeric test password without symbols? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `ascii_art_generator`, `count_vowels`, `convert_units`, `convert_currency`, `generate_password`.
**Ordering constraints:** `ascii_art_generator` before `count_vowels`; `count_vowels` before `convert_units`; `convert_units` before `convert_currency`; `convert_currency` before `generate_password`.
**Checks:** shape circle word banana; expected 3 value 68; F to C; expected 20 amount 100; USD to EUR; retrieved rate date length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Installed skill counts a/e/i/o/u only
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0642 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you use my vowel counter to count the vowels in banana; then use my installed stick-figure tool to draw a person in ASCII; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then generate a 20-character alphanumeric test password without symbols; then flip a coin using real randomness? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `count_vowels`, `human_shape`, `business_days_between`, `generate_password`, `random_pick`.
**Ordering constraints:** `count_vowels` before `human_shape`; `human_shape` before `business_days_between`; `business_days_between` before `generate_password`; `generate_password` before `random_pick`.
**Checks:** word banana; expected 3 no arguments; fixed figure start 2026-09-01; end 2026-09-10; both endpoints inclusive length 20; symbols false no options; Heads or Tails Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill counts a/e/i/o/u only
- Installed skill tool; no image-generation API
- Installed skill; expected 8 weekdays
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0643 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you use my installed stick-figure tool to draw a person in ASCII; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then load the full instructions for the installed interview-me skill, without starting the interview yet; then list my installed skills and whether they are enabled; then draw a triangle using the installed ASCII shape generator? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `human_shape`, `business_days_between`, `use_skill`, `wisp_skills`, `ascii_art_generator`.
**Ordering constraints:** `human_shape` before `business_days_between`; `business_days_between` before `use_skill`; `use_skill` before `wisp_skills`; `wisp_skills` before `ascii_art_generator`.
**Checks:** no arguments; fixed figure start 2026-09-01; end 2026-09-10; both endpoints inclusive name interview-me; load only if not already injected inventory only; no skill execution shape triangle; not raster image Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Installed skill; expected 8 weekdays
- Skill exists but its body is not yet in this synthetic turn's context
- Four callable skill fixtures plus instruction-only skills
- Installed shape-generator script available through intercepted skill call

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0644 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you use my installed stick-figure tool to draw a person in ASCII; then use my vowel counter to count the vowels in banana; then convert 100 US dollars to euros using the latest available rate and state its date; then calculate 18 percent of 64.50 exactly; then list my installed skills and whether they are enabled? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `human_shape`, `count_vowels`, `convert_currency`, `calculate`, `wisp_skills`.
**Ordering constraints:** `human_shape` before `count_vowels`; `count_vowels` before `convert_currency`; `convert_currency` before `calculate`; `calculate` before `wisp_skills`.
**Checks:** no arguments; fixed figure word banana; expected 3 amount 100; USD to EUR; retrieved rate date expression equivalent to 0.18*64.50; result 11.61 inventory only; no skill execution Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Installed skill counts a/e/i/o/u only
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- No external data needed
- Four callable skill fixtures plus instruction-only skills

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0645 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you use my installed stick-figure tool to draw a person in ASCII; then list my installed skills and whether they are enabled; then load the full instructions for the installed interview-me skill, without starting the interview yet; then pick two different choices randomly from tea, coffee, and water; then generate a 20-character alphanumeric test password without symbols? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `human_shape`, `wisp_skills`, `use_skill`, `random_pick`, `generate_password`.
**Ordering constraints:** `human_shape` before `wisp_skills`; `wisp_skills` before `use_skill`; `use_skill` before `random_pick`; `random_pick` before `generate_password`.
**Checks:** no arguments; fixed figure inventory only; no skill execution name interview-me; load only if not already injected options exact; count 2; no repeated choice length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Installed skill tool; no image-generation API
- Four callable skill fixtures plus instruction-only skills
- Skill exists but its body is not yet in this synthetic turn's context
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0646 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you flip a coin using real randomness; then convert 100 US dollars to euros using the latest available rate and state its date; then list my installed skills and whether they are enabled; then draw a square using my ASCII shape tool; then generate a 20-character alphanumeric test password without symbols? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `random_pick`, `convert_currency`, `wisp_skills`, `ascii_art_generator`, `generate_password`.
**Ordering constraints:** `random_pick` before `convert_currency`; `convert_currency` before `wisp_skills`; `wisp_skills` before `ascii_art_generator`; `ascii_art_generator` before `generate_password`.
**Checks:** no options; Heads or Tails amount 100; USD to EUR; retrieved rate date inventory only; no skill execution shape square length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Four callable skill fixtures plus instruction-only skills
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0647 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you flip a coin using real randomness; then load the full instructions for the installed interview-me skill, without starting the interview yet; then draw a square using my ASCII shape tool; then generate a 20-character alphanumeric test password without symbols; then list my installed skills and whether they are enabled? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `random_pick`, `use_skill`, `ascii_art_generator`, `generate_password`, `wisp_skills`.
**Ordering constraints:** `random_pick` before `use_skill`; `use_skill` before `ascii_art_generator`; `ascii_art_generator` before `generate_password`; `generate_password` before `wisp_skills`.
**Checks:** no options; Heads or Tails name interview-me; load only if not already injected shape square length 20; symbols false inventory only; no skill execution Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- OS randomness intercepted; result one offered option; variant-specific state must satisfy: no options; Heads or Tails
- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false
- Four callable skill fixtures plus instruction-only skills

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0648 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you load the full instructions for the installed interview-me skill, without starting the interview yet; then draw a square using my ASCII shape tool; then use my weekday-counting tool for September 1 through September 10, 2026, inclusive, ignoring holidays; then use my vowel counter to count the vowels in banana; then use my installed stick-figure tool to draw a person in ASCII? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `use_skill`, `ascii_art_generator`, `business_days_between`, `count_vowels`, `human_shape`.
**Ordering constraints:** `use_skill` before `ascii_art_generator`; `ascii_art_generator` before `business_days_between`; `business_days_between` before `count_vowels`; `count_vowels` before `human_shape`.
**Checks:** name interview-me; load only if not already injected shape square start 2026-09-01; end 2026-09-10; both endpoints inclusive word banana; expected 3 no arguments; fixed figure Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape square
- Installed skill; expected 8 weekdays
- Installed skill counts a/e/i/o/u only
- Installed skill tool; no image-generation API

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0649 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you load the full instructions for the installed interview-me skill, without starting the interview yet; then pick two different choices randomly from tea, coffee, and water; then list my installed skills and whether they are enabled; then draw a circle using my ASCII shape tool; then generate a 20-character alphanumeric test password without symbols? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `use_skill`, `random_pick`, `wisp_skills`, `ascii_art_generator`, `generate_password`.
**Ordering constraints:** `use_skill` before `random_pick`; `random_pick` before `wisp_skills`; `wisp_skills` before `ascii_art_generator`; `ascii_art_generator` before `generate_password`.
**Checks:** name interview-me; load only if not already injected options exact; count 2; no repeated choice inventory only; no skill execution shape circle length 20; symbols false Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Skill exists but its body is not yet in this synthetic turn's context
- OS randomness intercepted; result one offered option; variant-specific state must satisfy: options exact; count 2; no repeated choice
- Four callable skill fixtures plus instruction-only skills
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Synthetic secret output; redact result in reports; variant-specific state must satisfy: length 20; symbols false

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0650 · Colloquial with interruptions

**Focus:** Use existing deterministic tools and installed skills instead of guessing or rebuilding them.

**Prompt:**

Could you list my installed skills and whether they are enabled; then use my vowel counter to count the vowels in banana; then draw a circle using my ASCII shape tool; then use my installed stick-figure tool to draw a person in ASCII; then load the full instructions for the installed interview-me skill, without starting the interview yet? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `wisp_skills`, `count_vowels`, `ascii_art_generator`, `human_shape`, `use_skill`.
**Ordering constraints:** `wisp_skills` before `count_vowels`; `count_vowels` before `ascii_art_generator`; `ascii_art_generator` before `human_shape`; `human_shape` before `use_skill`.
**Checks:** inventory only; no skill execution word banana; expected 3 shape circle no arguments; fixed figure name interview-me; load only if not already injected Use existing deterministic tools and installed skills instead of guessing or rebuilding them.
**Accepted equivalents:** No redundant use_skill call is required if the exact requested skill body is already injected; evaluator must verify the loaded body, not accept an unsupported claim.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Four callable skill fixtures plus instruction-only skills
- Installed skill counts a/e/i/o/u only
- Installed shape-generator script available through intercepted skill call; variant-specific state must satisfy: shape circle
- Installed skill tool; no image-generation API
- Skill exists but its body is not yet in this synthetic turn's context

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
