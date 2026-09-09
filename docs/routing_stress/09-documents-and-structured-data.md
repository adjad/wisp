# Review only — no tests run

## 09. Documents and structured data

Turn known evidence into the right output format without inventing file contents.

### WRS-0401 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0401/Route proposal.txt; then zip the folder /tmp/wisp-routing-fixtures/wrs-0401/keep into /tmp/wisp-routing-fixtures/wrs-0401/output/keep.zip; then convert 100 US dollars to euros using the latest available rate and state its date; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0401/output/review.aiff; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0401/output/brief.docx.

**Required tools:** `read_file`, `archive_files`, `convert_currency`, `text_to_speech`, `write_document`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `archive_files`; `archive_files` before `convert_currency`; `convert_currency` before `text_to_speech`; `text_to_speech` before `write_document`.
**Checks:** exact path; file content, not guessed summary paths contains keep; archive_path exact; leave inputs amount 100; USD to EUR; retrieved rate date text exact; save_to exact; save instead of speaking Real docx at exact output path; contents grounded in read_file result. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic folder and absent output archive
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; document creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0402 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0402/Route proposal.txt; then calculate the total cost using the quantity and per-sample price in that proposal; then convert 100 US dollars to euros using the latest available rate and state its date; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0402/output/brief.docx; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0402/output/review.aiff.

**Required tools:** `read_file`, `calculate`, `convert_currency`, `write_document`, `text_to_speech`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `write_document`; `calculate` before `convert_currency`; `convert_currency` before `write_document`; `write_document` before `text_to_speech`.
**Checks:** exact path; file content, not guessed summary Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. amount 100; USD to EUR; retrieved rate date Real docx at exact output path; contents grounded in read_file result. text exact; save_to exact; save instead of speaking Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- No external data needed
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; document creation intercepted
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0403 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0403/Route proposal.txt; then convert 100 US dollars to euros using the latest available rate and state its date; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0403/output/review.aiff; then create /tmp/wisp-routing-fixtures/wrs-0403/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0403/output/status.txt.

**Required tools:** `read_file`, `convert_currency`, `text_to_speech`, `spreadsheet_ops`, `write_file`.
**Ordering constraints:** `read_file` before `convert_currency`; `convert_currency` before `text_to_speech`; `text_to_speech` before `spreadsheet_ops`; `spreadsheet_ops` before `write_file`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date text exact; save_to exact; save instead of speaking real xlsx; headers and row types correct exact path/content; text file; not Notes Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; workbook creation intercepted
- Synthetic output directory writable; file absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0404 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0404/Route proposal.txt; then convert 100 US dollars to euros using the latest available rate and state its date; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0404/output/brief.docx; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0404/output/status.txt; then convert 68 degrees Fahrenheit to Celsius.

**Required tools:** `read_file`, `convert_currency`, `write_document`, `write_file`, `convert_units`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `convert_currency`; `convert_currency` before `write_document`; `write_document` before `write_file`; `write_file` before `convert_units`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date Real docx at exact output path; contents grounded in read_file result. exact path/content; text file; not Notes value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; document creation intercepted
- Synthetic output directory writable; file absent
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0405 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0405/Route proposal.txt; then convert 68 degrees Fahrenheit to Celsius; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0405/output/status.txt; then zip the folder /tmp/wisp-routing-fixtures/wrs-0405/keep into /tmp/wisp-routing-fixtures/wrs-0405/output/keep.zip; then calculate the total cost using the quantity and per-sample price in that proposal.

**Required tools:** `read_file`, `convert_units`, `write_file`, `archive_files`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `convert_units`; `convert_units` before `write_file`; `write_file` before `archive_files`; `archive_files` before `calculate`.
**Checks:** exact path; file content, not guessed summary value 68; F to C; expected 20 exact path/content; text file; not Notes paths contains keep; archive_path exact; leave inputs Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic output directory writable; file absent
- Synthetic folder and absent output archive
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0406 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0406/Route proposal.txt; then create /tmp/wisp-routing-fixtures/wrs-0406/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then convert 100 US dollars to euros using the latest available rate and state its date; then convert 68 degrees Fahrenheit to Celsius.

**Required tools:** `read_file`, `spreadsheet_ops`, `create_note`, `convert_currency`, `convert_units`.
**Ordering constraints:** `read_file` before `spreadsheet_ops`; `spreadsheet_ops` before `create_note`; `create_note` before `convert_currency`; `convert_currency` before `convert_units`.
**Checks:** exact path; file content, not guessed summary real xlsx; headers and row types correct title Route groceries; checklist true; disclose bullet-list limitation if relevant amount 100; USD to EUR; retrieved rate date value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; workbook creation intercepted
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0407 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0407/Route proposal.txt; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0407/output/review.aiff; then convert 68 degrees Fahrenheit to Celsius; then zip the folder /tmp/wisp-routing-fixtures/wrs-0407/keep into /tmp/wisp-routing-fixtures/wrs-0407/output/keep.zip; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0407/output/brief.docx.

**Required tools:** `read_file`, `text_to_speech`, `convert_units`, `archive_files`, `write_document`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `text_to_speech`; `text_to_speech` before `convert_units`; `convert_units` before `archive_files`; `archive_files` before `write_document`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking value 68; F to C; expected 20 paths contains keep; archive_path exact; leave inputs Real docx at exact output path; contents grounded in read_file result. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic folder and absent output archive
- Synthetic output absent; document creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0408 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0408/Route proposal.txt; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0408/output/review.aiff; then create /tmp/wisp-routing-fixtures/wrs-0408/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then calculate the total cost using the quantity and per-sample price in that proposal; then convert 100 US dollars to euros using the latest available rate and state its date.

**Required tools:** `read_file`, `text_to_speech`, `spreadsheet_ops`, `calculate`, `convert_currency`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `text_to_speech`; `text_to_speech` before `spreadsheet_ops`; `spreadsheet_ops` before `calculate`; `calculate` before `convert_currency`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking real xlsx; headers and row types correct Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. amount 100; USD to EUR; retrieved rate date Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; workbook creation intercepted
- No external data needed
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0409 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0409/Route proposal.txt; then say "Route review is ready." aloud; then create /tmp/wisp-routing-fixtures/wrs-0409/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0409/output/brief.docx; then calculate the total cost using the quantity and per-sample price in that proposal.

**Required tools:** `read_file`, `text_to_speech`, `spreadsheet_ops`, `write_document`, `calculate`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`; `read_file` before `text_to_speech`; `text_to_speech` before `spreadsheet_ops`; `spreadsheet_ops` before `write_document`; `write_document` before `calculate`.
**Checks:** exact path; file content, not guessed summary text exact; no save unless asked real xlsx; headers and row types correct Real docx at exact output path; contents grounded in read_file result. Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output
- Synthetic output absent; workbook creation intercepted
- Synthetic output absent; document creation intercepted
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0410 · Explicit sequence

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Please do these in this order: read the text of /tmp/wisp-routing-fixtures/wrs-0410/Route proposal.txt; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0410/output/brief.docx; then create /tmp/wisp-routing-fixtures/wrs-0410/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then calculate the total cost using the quantity and per-sample price in that proposal; then convert 68 degrees Fahrenheit to Celsius.

**Required tools:** `read_file`, `write_document`, `spreadsheet_ops`, `calculate`, `convert_units`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`; `write_document` before `spreadsheet_ops`; `spreadsheet_ops` before `calculate`; `calculate` before `convert_units`.
**Checks:** exact path; file content, not guessed summary Real docx at exact output path; contents grounded in read_file result. real xlsx; headers and row types correct Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; document creation intercepted
- Synthetic output absent; workbook creation intercepted
- No external data needed
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0411 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0411/Route proposal.txt. Zip the folder /tmp/wisp-routing-fixtures/wrs-0411/keep into /tmp/wisp-routing-fixtures/wrs-0411/output/keep.zip. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0411/output/review.aiff. Convert /tmp/wisp-routing-fixtures/wrs-0411/input/sample.rtf to a Word docx file. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0411/output/status.txt. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `archive_files`, `text_to_speech`, `convert_file`, `write_file`.
**Checks:** exact path; file content, not guessed summary paths contains keep; archive_path exact; leave inputs text exact; save_to exact; save instead of speaking to_format docx; preserve input; new output exact path/content; text file; not Notes Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic folder and absent output archive
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic output directory writable; file absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0412 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0412/Route proposal.txt. Zip the folder /tmp/wisp-routing-fixtures/wrs-0412/keep into /tmp/wisp-routing-fixtures/wrs-0412/output/keep.zip. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0412/output/status.txt. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0412/output/review.aiff. Calculate the total cost using the quantity and per-sample price in that proposal. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `archive_files`, `write_file`, `text_to_speech`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary paths contains keep; archive_path exact; leave inputs exact path/content; text file; not Notes text exact; save_to exact; save instead of speaking Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic folder and absent output archive
- Synthetic output directory writable; file absent
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0413 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0413/Route proposal.txt. Calculate the total cost using the quantity and per-sample price in that proposal. Convert 68 degrees Fahrenheit to Celsius. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0413/output/review.aiff. Convert /tmp/wisp-routing-fixtures/wrs-0413/input/sample.rtf to a Word docx file. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `calculate`, `convert_units`, `text_to_speech`, `convert_file`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. value 68; F to C; expected 20 text exact; save_to exact; save instead of speaking to_format docx; preserve input; new output Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- No external data needed
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0414 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0414/Route proposal.txt. Calculate the total cost using the quantity and per-sample price in that proposal. Create /tmp/wisp-routing-fixtures/wrs-0414/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Convert 68 degrees Fahrenheit to Celsius. Convert 100 US dollars to euros using the latest available rate and state its date. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `calculate`, `spreadsheet_ops`, `convert_units`, `convert_currency`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. real xlsx; headers and row types correct value 68; F to C; expected 20 amount 100; USD to EUR; retrieved rate date Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- No external data needed
- Synthetic output absent; workbook creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0415 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0415/Route proposal.txt. Convert /tmp/wisp-routing-fixtures/wrs-0415/input/sample.rtf to a Word docx file. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0415/output/status.txt. Convert 100 US dollars to euros using the latest available rate and state its date. Calculate the total cost using the quantity and per-sample price in that proposal. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `convert_file`, `write_file`, `convert_currency`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary to_format docx; preserve input; new output exact path/content; text file; not Notes amount 100; USD to EUR; retrieved rate date Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic output directory writable; file absent
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0416 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0416/Route proposal.txt. Convert 68 degrees Fahrenheit to Celsius. Calculate the total cost using the quantity and per-sample price in that proposal. Create /tmp/wisp-routing-fixtures/wrs-0416/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Zip the folder /tmp/wisp-routing-fixtures/wrs-0416/keep into /tmp/wisp-routing-fixtures/wrs-0416/output/keep.zip. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `convert_units`, `calculate`, `spreadsheet_ops`, `archive_files`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary value 68; F to C; expected 20 Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. real xlsx; headers and row types correct paths contains keep; archive_path exact; leave inputs Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- No external data needed
- Synthetic output absent; workbook creation intercepted
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0417 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0417/Route proposal.txt. Convert 68 degrees Fahrenheit to Celsius. Convert 100 US dollars to euros using the latest available rate and state its date. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0417/output/review.aiff. Calculate the total cost using the quantity and per-sample price in that proposal. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `convert_units`, `convert_currency`, `text_to_speech`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary value 68; F to C; expected 20 amount 100; USD to EUR; retrieved rate date text exact; save_to exact; save instead of speaking Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0418 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0418/Route proposal.txt. Convert 180 pounds to kilograms. Convert 100 US dollars to euros using the latest available rate and state its date. Say "Route review is ready." aloud. Create /tmp/wisp-routing-fixtures/wrs-0418/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `convert_units`, `convert_currency`, `text_to_speech`, `spreadsheet_ops`.
**Checks:** exact path; file content, not guessed summary value 180; from_unit lb; to_unit kg amount 100; USD to EUR; retrieved rate date text exact; no save unless asked real xlsx; headers and row types correct Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Unit conversion implementation available
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic speech process; no real speaker output
- Synthetic output absent; workbook creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0419 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0419/Route proposal.txt. Convert 68 degrees Fahrenheit to Celsius. Create /tmp/wisp-routing-fixtures/wrs-0419/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0419/output/status.txt. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0419/output/review.aiff. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `convert_units`, `spreadsheet_ops`, `write_file`, `text_to_speech`.
**Checks:** exact path; file content, not guessed summary value 68; F to C; expected 20 real xlsx; headers and row types correct exact path/content; text file; not Notes text exact; save_to exact; save instead of speaking Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic output absent; workbook creation intercepted
- Synthetic output directory writable; file absent
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0420 · Natural compound request

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

I have a few things to finish. Read the text of /tmp/wisp-routing-fixtures/wrs-0420/Route proposal.txt. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0420/output/brief.docx. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0420/output/review.aiff. Convert 100 US dollars to euros using the latest available rate and state its date. Convert 68 degrees Fahrenheit to Celsius. Keep the results separate so I can tell what came from where.

**Required tools:** `read_file`, `write_document`, `text_to_speech`, `convert_currency`, `convert_units`.
**Ordering constraints:** `read_file` before `write_document`.
**Checks:** exact path; file content, not guessed summary Real docx at exact output path; contents grounded in read_file result. text exact; save_to exact; save instead of speaking amount 100; USD to EUR; retrieved rate date value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; document creation intercepted
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0421 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0421/Route proposal.txt; then convert 100 US dollars to euros using the latest available rate and state its date; then calculate the total cost using the quantity and per-sample price in that proposal; then create /tmp/wisp-routing-fixtures/wrs-0421/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then convert 68 degrees Fahrenheit to Celsius. Leave everything else unchanged.

**Required tools:** `read_file`, `convert_currency`, `calculate`, `spreadsheet_ops`, `convert_units`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `convert_currency`; `convert_currency` before `calculate`; `calculate` before `spreadsheet_ops`; `spreadsheet_ops` before `convert_units`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. real xlsx; headers and row types correct value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- No external data needed
- Synthetic output absent; workbook creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0422 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0422/Route proposal.txt; then convert 100 US dollars to euros using the latest available rate and state its date; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0422/output/brief.docx; then calculate the total cost using the quantity and per-sample price in that proposal; then create /tmp/wisp-routing-fixtures/wrs-0422/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Leave everything else unchanged.

**Required tools:** `read_file`, `convert_currency`, `write_document`, `calculate`, `spreadsheet_ops`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`; `read_file` before `convert_currency`; `convert_currency` before `write_document`; `write_document` before `calculate`; `calculate` before `spreadsheet_ops`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date Real docx at exact output path; contents grounded in read_file result. Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. real xlsx; headers and row types correct Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; document creation intercepted
- No external data needed
- Synthetic output absent; workbook creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0423 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0423/Route proposal.txt; then convert /tmp/wisp-routing-fixtures/wrs-0423/input/sample.rtf to a Word docx file; then zip the folder /tmp/wisp-routing-fixtures/wrs-0423/keep into /tmp/wisp-routing-fixtures/wrs-0423/output/keep.zip; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0423/output/brief.docx; then create /tmp/wisp-routing-fixtures/wrs-0423/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Leave everything else unchanged.

**Required tools:** `read_file`, `convert_file`, `archive_files`, `write_document`, `spreadsheet_ops`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `convert_file`; `convert_file` before `archive_files`; `archive_files` before `write_document`; `write_document` before `spreadsheet_ops`.
**Checks:** exact path; file content, not guessed summary to_format docx; preserve input; new output paths contains keep; archive_path exact; leave inputs Real docx at exact output path; contents grounded in read_file result. real xlsx; headers and row types correct Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic folder and absent output archive
- Synthetic output absent; document creation intercepted
- Synthetic output absent; workbook creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0424 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0424/Route proposal.txt; then create /tmp/wisp-routing-fixtures/wrs-0424/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then convert 100 US dollars to euros using the latest available rate and state its date; then zip the folder /tmp/wisp-routing-fixtures/wrs-0424/keep into /tmp/wisp-routing-fixtures/wrs-0424/output/keep.zip; then calculate the total cost using the quantity and per-sample price in that proposal. Leave everything else unchanged.

**Required tools:** `read_file`, `spreadsheet_ops`, `convert_currency`, `archive_files`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `spreadsheet_ops`; `spreadsheet_ops` before `convert_currency`; `convert_currency` before `archive_files`; `archive_files` before `calculate`.
**Checks:** exact path; file content, not guessed summary real xlsx; headers and row types correct amount 100; USD to EUR; retrieved rate date paths contains keep; archive_path exact; leave inputs Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; workbook creation intercepted
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic folder and absent output archive
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0425 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0425/Route proposal.txt; then create /tmp/wisp-routing-fixtures/wrs-0425/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then convert 68 degrees Fahrenheit to Celsius; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0425/output/brief.docx; then zip the folder /tmp/wisp-routing-fixtures/wrs-0425/keep into /tmp/wisp-routing-fixtures/wrs-0425/output/keep.zip. Leave everything else unchanged.

**Required tools:** `read_file`, `spreadsheet_ops`, `convert_units`, `write_document`, `archive_files`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `spreadsheet_ops`; `spreadsheet_ops` before `convert_units`; `convert_units` before `write_document`; `write_document` before `archive_files`.
**Checks:** exact path; file content, not guessed summary real xlsx; headers and row types correct value 68; F to C; expected 20 Real docx at exact output path; contents grounded in read_file result. paths contains keep; archive_path exact; leave inputs Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; workbook creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic output absent; document creation intercepted
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0426 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0426/Route proposal.txt; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0426/output/review.aiff; then convert /tmp/wisp-routing-fixtures/wrs-0426/input/sample.rtf to a Word docx file; then create /tmp/wisp-routing-fixtures/wrs-0426/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0426/output/status.txt. Leave everything else unchanged.

**Required tools:** `read_file`, `text_to_speech`, `convert_file`, `spreadsheet_ops`, `write_file`.
**Ordering constraints:** `read_file` before `text_to_speech`; `text_to_speech` before `convert_file`; `convert_file` before `spreadsheet_ops`; `spreadsheet_ops` before `write_file`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking to_format docx; preserve input; new output real xlsx; headers and row types correct exact path/content; text file; not Notes Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic output absent; workbook creation intercepted
- Synthetic output directory writable; file absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0427 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0427/Route proposal.txt; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0427/output/brief.docx; then convert 180 pounds to kilograms; then say "Route review is ready." aloud; then calculate the total cost using the quantity and per-sample price in that proposal. Leave everything else unchanged.

**Required tools:** `read_file`, `write_document`, `convert_units`, `text_to_speech`, `calculate`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`; `write_document` before `convert_units`; `convert_units` before `text_to_speech`; `text_to_speech` before `calculate`.
**Checks:** exact path; file content, not guessed summary Real docx at exact output path; contents grounded in read_file result. value 180; from_unit lb; to_unit kg text exact; no save unless asked Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; document creation intercepted
- Unit conversion implementation available
- Synthetic speech process; no real speaker output
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0428 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0428/Route proposal.txt; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0428/output/brief.docx; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0428/output/review.aiff; then convert 100 US dollars to euros using the latest available rate and state its date; then create /tmp/wisp-routing-fixtures/wrs-0428/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Leave everything else unchanged.

**Required tools:** `read_file`, `write_document`, `text_to_speech`, `convert_currency`, `spreadsheet_ops`.
**Ordering constraints:** `read_file` before `write_document`; `write_document` before `text_to_speech`; `text_to_speech` before `convert_currency`; `convert_currency` before `spreadsheet_ops`.
**Checks:** exact path; file content, not guessed summary Real docx at exact output path; contents grounded in read_file result. text exact; save_to exact; save instead of speaking amount 100; USD to EUR; retrieved rate date real xlsx; headers and row types correct Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; document creation intercepted
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; workbook creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0429 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0429/Route proposal.txt; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0429/output/status.txt; then calculate the total cost using the quantity and per-sample price in that proposal; then convert 100 US dollars to euros using the latest available rate and state its date; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0429/output/brief.docx. Leave everything else unchanged.

**Required tools:** `read_file`, `write_file`, `calculate`, `convert_currency`, `write_document`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `write_document`; `read_file` before `write_file`; `write_file` before `calculate`; `calculate` before `convert_currency`; `convert_currency` before `write_document`.
**Checks:** exact path; file content, not guessed summary exact path/content; text file; not Notes Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. amount 100; USD to EUR; retrieved rate date Real docx at exact output path; contents grounded in read_file result. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output directory writable; file absent
- No external data needed
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; document creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0430 · Scoped execution

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

For these tasks, use only the named sources and targets: read the text of /tmp/wisp-routing-fixtures/wrs-0430/Route proposal.txt; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0430/output/status.txt; then convert /tmp/wisp-routing-fixtures/wrs-0430/input/sample.rtf to a Word docx file; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0430/output/review.aiff; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0430/output/brief.docx. Leave everything else unchanged.

**Required tools:** `read_file`, `write_file`, `convert_file`, `text_to_speech`, `write_document`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `write_file`; `write_file` before `convert_file`; `convert_file` before `text_to_speech`; `text_to_speech` before `write_document`.
**Checks:** exact path; file content, not guessed summary exact path/content; text file; not Notes to_format docx; preserve input; new output text exact; save_to exact; save instead of speaking Real docx at exact output path; contents grounded in read_file result. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output directory writable; file absent
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; document creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0431 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0431/Route proposal.txt. Zip the folder /tmp/wisp-routing-fixtures/wrs-0431/keep into /tmp/wisp-routing-fixtures/wrs-0431/output/keep.zip. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0431/output/brief.docx. Calculate the total cost using the quantity and per-sample price in that proposal. Convert 100 US dollars to euros using the latest available rate and state its date. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `archive_files`, `write_document`, `calculate`, `convert_currency`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary paths contains keep; archive_path exact; leave inputs Real docx at exact output path; contents grounded in read_file result. Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. amount 100; USD to EUR; retrieved rate date Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic folder and absent output archive
- Synthetic output absent; document creation intercepted
- No external data needed
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0432 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0432/Route proposal.txt. Convert 100 US dollars to euros using the latest available rate and state its date. Calculate the total cost using the quantity and per-sample price in that proposal. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0432/output/brief.docx. Convert 68 degrees Fahrenheit to Celsius. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `convert_currency`, `calculate`, `write_document`, `convert_units`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `write_document`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Real docx at exact output path; contents grounded in read_file result. value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- No external data needed
- Synthetic output absent; document creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0433 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0433/Route proposal.txt. Convert 100 US dollars to euros using the latest available rate and state its date. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0433/output/brief.docx. Create /tmp/wisp-routing-fixtures/wrs-0433/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Convert 68 degrees Fahrenheit to Celsius. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `convert_currency`, `write_document`, `spreadsheet_ops`, `convert_units`.
**Ordering constraints:** `read_file` before `write_document`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date Real docx at exact output path; contents grounded in read_file result. real xlsx; headers and row types correct value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; document creation intercepted
- Synthetic output absent; workbook creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0434 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0434/Route proposal.txt. Convert 100 US dollars to euros using the latest available rate and state its date. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0434/output/status.txt. Convert 68 degrees Fahrenheit to Celsius. Calculate the total cost using the quantity and per-sample price in that proposal. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `convert_currency`, `write_file`, `convert_units`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary amount 100; USD to EUR; retrieved rate date exact path/content; text file; not Notes value 68; F to C; expected 20 Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output directory writable; file absent
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0435 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0435/Route proposal.txt. Convert /tmp/wisp-routing-fixtures/wrs-0435/input/sample.rtf to a Word docx file. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0435/output/review.aiff. Convert 100 US dollars to euros using the latest available rate and state its date. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0435/output/brief.docx. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `convert_file`, `text_to_speech`, `convert_currency`, `write_document`.
**Ordering constraints:** `read_file` before `write_document`.
**Checks:** exact path; file content, not guessed summary to_format docx; preserve input; new output text exact; save_to exact; save instead of speaking amount 100; USD to EUR; retrieved rate date Real docx at exact output path; contents grounded in read_file result. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic output absent; document creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0436 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0436/Route proposal.txt. Create /tmp/wisp-routing-fixtures/wrs-0436/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Convert 68 degrees Fahrenheit to Celsius. Convert /tmp/wisp-routing-fixtures/wrs-0436/input/sample.rtf to a Word docx file. Zip the folder /tmp/wisp-routing-fixtures/wrs-0436/keep into /tmp/wisp-routing-fixtures/wrs-0436/output/keep.zip. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `spreadsheet_ops`, `convert_units`, `convert_file`, `archive_files`.
**Checks:** exact path; file content, not guessed summary real xlsx; headers and row types correct value 68; F to C; expected 20 to_format docx; preserve input; new output paths contains keep; archive_path exact; leave inputs Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; workbook creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0437 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0437/Route proposal.txt. Create /tmp/wisp-routing-fixtures/wrs-0437/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0437/output/brief.docx. Convert 180 pounds to kilograms. Convert 100 US dollars to euros using the latest available rate and state its date. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `spreadsheet_ops`, `write_document`, `convert_units`, `convert_currency`.
**Ordering constraints:** `read_file` before `write_document`.
**Checks:** exact path; file content, not guessed summary real xlsx; headers and row types correct Real docx at exact output path; contents grounded in read_file result. value 180; from_unit lb; to_unit kg amount 100; USD to EUR; retrieved rate date Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; workbook creation intercepted
- Synthetic output absent; document creation intercepted
- Unit conversion implementation available
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0438 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0438/Route proposal.txt. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0438/output/review.aiff. Convert /tmp/wisp-routing-fixtures/wrs-0438/input/sample.rtf to a Word docx file. Calculate the total cost using the quantity and per-sample price in that proposal. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0438/output/brief.docx. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `text_to_speech`, `convert_file`, `calculate`, `write_document`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `write_document`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking to_format docx; preserve input; new output Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Real docx at exact output path; contents grounded in read_file result. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- No external data needed
- Synthetic output absent; document creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0439 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0439/Route proposal.txt. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0439/output/review.aiff. Convert 68 degrees Fahrenheit to Celsius. Convert /tmp/wisp-routing-fixtures/wrs-0439/input/sample.rtf to a Word docx file. Convert 100 US dollars to euros using the latest available rate and state its date. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `text_to_speech`, `convert_units`, `convert_file`, `convert_currency`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking value 68; F to C; expected 20 to_format docx; preserve input; new output amount 100; USD to EUR; retrieved rate date Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0440 · Late constraints

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Read the text of /tmp/wisp-routing-fixtures/wrs-0440/Route proposal.txt. Save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0440/output/review.aiff. Write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0440/output/brief.docx. Create /tmp/wisp-routing-fixtures/wrs-0440/output/costs.xlsx with headers Item and Cost, and one row Sample and 18. Calculate the total cost using the quantity and per-sample price in that proposal. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `read_file`, `text_to_speech`, `write_document`, `spreadsheet_ops`, `calculate`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking Real docx at exact output path; contents grounded in read_file result. real xlsx; headers and row types correct Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; document creation intercepted
- Synthetic output absent; workbook creation intercepted
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0441 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0441/Route proposal.txt; then zip the folder /tmp/wisp-routing-fixtures/wrs-0441/keep into /tmp/wisp-routing-fixtures/wrs-0441/output/keep.zip; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0441/output/brief.docx; then create /tmp/wisp-routing-fixtures/wrs-0441/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0441/output/review.aiff? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `archive_files`, `write_document`, `spreadsheet_ops`, `text_to_speech`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `archive_files`; `archive_files` before `write_document`; `write_document` before `spreadsheet_ops`; `spreadsheet_ops` before `text_to_speech`.
**Checks:** exact path; file content, not guessed summary paths contains keep; archive_path exact; leave inputs Real docx at exact output path; contents grounded in read_file result. real xlsx; headers and row types correct text exact; save_to exact; save instead of speaking Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic folder and absent output archive
- Synthetic output absent; document creation intercepted
- Synthetic output absent; workbook creation intercepted
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0442 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0442/Route proposal.txt; then calculate the total cost using the quantity and per-sample price in that proposal; then convert 68 degrees Fahrenheit to Celsius; then create /tmp/wisp-routing-fixtures/wrs-0442/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0442/output/review.aiff? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `calculate`, `convert_units`, `spreadsheet_ops`, `text_to_speech`.
**Ordering constraints:** `read_file` before `calculate`; `calculate` before `convert_units`; `convert_units` before `spreadsheet_ops`; `spreadsheet_ops` before `text_to_speech`.
**Checks:** exact path; file content, not guessed summary Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. value 68; F to C; expected 20 real xlsx; headers and row types correct text exact; save_to exact; save instead of speaking Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- No external data needed
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic output absent; workbook creation intercepted
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0443 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0443/Route proposal.txt; then calculate the total cost using the quantity and per-sample price in that proposal; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0443/output/status.txt; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0443/output/review.aiff; then create /tmp/wisp-routing-fixtures/wrs-0443/output/costs.xlsx with headers Item and Cost, and one row Sample and 18? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `calculate`, `write_file`, `text_to_speech`, `spreadsheet_ops`.
**Ordering constraints:** `read_file` before `calculate`; `calculate` before `write_file`; `write_file` before `text_to_speech`; `text_to_speech` before `spreadsheet_ops`.
**Checks:** exact path; file content, not guessed summary Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. exact path/content; text file; not Notes text exact; save_to exact; save instead of speaking real xlsx; headers and row types correct Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- No external data needed
- Synthetic output directory writable; file absent
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; workbook creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0444 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0444/Route proposal.txt; then convert /tmp/wisp-routing-fixtures/wrs-0444/input/sample.rtf to a Word docx file; then convert 100 US dollars to euros using the latest available rate and state its date; then zip the folder /tmp/wisp-routing-fixtures/wrs-0444/keep into /tmp/wisp-routing-fixtures/wrs-0444/output/keep.zip; then convert 68 degrees Fahrenheit to Celsius? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `convert_file`, `convert_currency`, `archive_files`, `convert_units`.
**Ordering constraints:** `read_file` before `convert_file`; `convert_file` before `convert_currency`; `convert_currency` before `archive_files`; `archive_files` before `convert_units`.
**Checks:** exact path; file content, not guessed summary to_format docx; preserve input; new output amount 100; USD to EUR; retrieved rate date paths contains keep; archive_path exact; leave inputs value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- Synthetic folder and absent output archive
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0445 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0445/Route proposal.txt; then create a Notes note called Route groceries with Milk and Bread as separate checklist lines; then zip the folder /tmp/wisp-routing-fixtures/wrs-0445/keep into /tmp/wisp-routing-fixtures/wrs-0445/output/keep.zip; then append "Bring a spare cable." to my existing Route packing note; then calculate the total cost using the quantity and per-sample price in that proposal? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `create_note`, `archive_files`, `append_note`, `calculate`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `create_note`; `create_note` before `archive_files`; `archive_files` before `append_note`; `append_note` before `calculate`.
**Checks:** exact path; file content, not guessed summary title Route groceries; checklist true; disclose bullet-list limitation if relevant paths contains keep; archive_path exact; leave inputs title Route packing; append exact line; preserve body Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- No existing Route ideas note; native Notes operation is intercepted; variant-specific state must satisfy: title Route groceries; checklist true; disclose bullet-list limitation if relevant
- Synthetic folder and absent output archive
- Unique existing Route packing note; backend cache may not refresh immediately
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0446 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0446/Route proposal.txt; then create /tmp/wisp-routing-fixtures/wrs-0446/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then calculate the total cost using the quantity and per-sample price in that proposal; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0446/output/brief.docx; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0446/output/review.aiff? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `spreadsheet_ops`, `calculate`, `write_document`, `text_to_speech`.
**Ordering constraints:** `read_file` before `calculate`; `read_file` before `write_document`; `read_file` before `spreadsheet_ops`; `spreadsheet_ops` before `calculate`; `calculate` before `write_document`; `write_document` before `text_to_speech`.
**Checks:** exact path; file content, not guessed summary real xlsx; headers and row types correct Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Real docx at exact output path; contents grounded in read_file result. text exact; save_to exact; save instead of speaking Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; workbook creation intercepted
- No external data needed
- Synthetic output absent; document creation intercepted
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0447 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0447/Route proposal.txt; then save speech saying "Route review is ready." to /tmp/wisp-routing-fixtures/wrs-0447/output/review.aiff; then create /tmp/wisp-routing-fixtures/wrs-0447/output/costs.xlsx with headers Item and Cost, and one row Sample and 18; then convert 68 degrees Fahrenheit to Celsius; then convert /tmp/wisp-routing-fixtures/wrs-0447/input/sample.rtf to a Word docx file? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `text_to_speech`, `spreadsheet_ops`, `convert_units`, `convert_file`.
**Ordering constraints:** `read_file` before `text_to_speech`; `text_to_speech` before `spreadsheet_ops`; `spreadsheet_ops` before `convert_units`; `convert_units` before `convert_file`.
**Checks:** exact path; file content, not guessed summary text exact; save_to exact; save instead of speaking real xlsx; headers and row types correct value 68; F to C; expected 20 to_format docx; preserve input; new output Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic speech process; no real speaker output; variant-specific state must satisfy: text exact; save_to exact; save instead of speaking
- Synthetic output absent; workbook creation intercepted
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0448 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0448/Route proposal.txt; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0448/output/brief.docx; then convert 180 pounds to kilograms; then convert 100 US dollars to euros using the latest available rate and state its date; then calculate the total cost using the quantity and per-sample price in that proposal? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `write_document`, `convert_units`, `convert_currency`, `calculate`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `calculate`; `write_document` before `convert_units`; `convert_units` before `convert_currency`; `convert_currency` before `calculate`.
**Checks:** exact path; file content, not guessed summary Real docx at exact output path; contents grounded in read_file result. value 180; from_unit lb; to_unit kg amount 100; USD to EUR; retrieved rate date Use retrieved proposal's 4 samples at $18; total $72; do not invent prices. Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; document creation intercepted
- Unit conversion implementation available
- Synthetic provider rate 0.90 EUR per USD with explicit retrieval date
- No external data needed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0449 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0449/Route proposal.txt; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0449/output/brief.docx; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0449/output/status.txt; then convert /tmp/wisp-routing-fixtures/wrs-0449/input/sample.rtf to a Word docx file; then convert 68 degrees Fahrenheit to Celsius? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `write_document`, `write_file`, `convert_file`, `convert_units`.
**Ordering constraints:** `read_file` before `write_document`; `write_document` before `write_file`; `write_file` before `convert_file`; `convert_file` before `convert_units`.
**Checks:** exact path; file content, not guessed summary Real docx at exact output path; contents grounded in read_file result. exact path/content; text file; not Notes to_format docx; preserve input; new output value 68; F to C; expected 20 Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output absent; document creation intercepted
- Synthetic output directory writable; file absent
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Unit conversion implementation available; variant-specific state must satisfy: value 68; F to C; expected 20

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0450 · Colloquial with interruptions

**Focus:** Turn known evidence into the right output format without inventing file contents.

**Prompt:**

Could you read the text of /tmp/wisp-routing-fixtures/wrs-0450/Route proposal.txt; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0450/output/status.txt; then convert /tmp/wisp-routing-fixtures/wrs-0450/input/sample.rtf to a Word docx file; then write a Word summary of the proposal you just read at /tmp/wisp-routing-fixtures/wrs-0450/output/brief.docx; then create /tmp/wisp-routing-fixtures/wrs-0450/output/costs.xlsx with headers Item and Cost, and one row Sample and 18? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `read_file`, `write_file`, `convert_file`, `write_document`, `spreadsheet_ops`.
**Ordering constraints:** `read_file` before `write_document`; `read_file` before `write_file`; `write_file` before `convert_file`; `convert_file` before `write_document`; `write_document` before `spreadsheet_ops`.
**Checks:** exact path; file content, not guessed summary exact path/content; text file; not Notes to_format docx; preserve input; new output Real docx at exact output path; contents grounded in read_file result. real xlsx; headers and row types correct Turn known evidence into the right output format without inventing file contents.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Fixture text is Route sample costs 18 dollars each; quantity 4; no instructions
- Synthetic output directory writable; file absent
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic output absent; document creation intercepted
- Synthetic output absent; workbook creation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
