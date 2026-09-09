# Review only — no tests run

## 08. File discovery, organization, and deletion

Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

### WRS-0351 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0351, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0351; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0351/keep/share.pdf; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0351/keep/share.pdf; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0351/keep at /tmp/wisp-routing-fixtures/wrs-0351/backup-copy.

**Required tools:** `list_dir`, `find_files`, `airdrop_file`, `tag_file`, `backup_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `airdrop_file`; `airdrop_file` before `tag_file`; `tag_file` before `backup_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0351; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0351; Spotlight search exact file; manual Finder/share handoff only color none; exact path exact source/destination; preserve source Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0351/Route proposal.txt exists in synthetic Spotlight results
- File exists; native reveal/share path intercepted
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path
- Destination absent; isolated filesystem fixture

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0352 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0352, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0352; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0352/keep at /tmp/wisp-routing-fixtures/wrs-0352/backup-copy; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0352/empty-receipts; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0352/input into /tmp/wisp-routing-fixtures/wrs-0352/images; I authorize that scoped move.

**Required tools:** `list_dir`, `find_files`, `backup_folder`, `create_folder`, `organize_files`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `backup_folder`; `backup_folder` before `create_folder`; `create_folder` before `organize_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0352; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0352; Spotlight search exact source/destination; preserve source exact path; empty folder pattern *.png; preview then confirm true; skip collisions; do not move PDF Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0352/Route proposal.txt exists in synthetic Spotlight results
- Destination absent; isolated filesystem fixture
- Target absent; synthetic filesystem only
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0353 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0353, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0353; then convert /tmp/wisp-routing-fixtures/wrs-0353/input/sample.rtf to a Word docx file; then show /tmp/wisp-routing-fixtures/wrs-0353/keep/share.pdf selected in Finder; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0353/input into /tmp/wisp-routing-fixtures/wrs-0353/images; I authorize that scoped move.

**Required tools:** `list_dir`, `find_files`, `convert_file`, `reveal_in_finder`, `organize_files`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `convert_file`; `convert_file` before `reveal_in_finder`; `reveal_in_finder` before `organize_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0353; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0353; Spotlight search to_format docx; preserve input; new output exact path; reveal only pattern *.png; preview then confirm true; skip collisions; do not move PDF Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0353/Route proposal.txt exists in synthetic Spotlight results
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic existing file and native reveal response
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0354 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0354, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0354; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0354/empty-receipts; then show /tmp/wisp-routing-fixtures/wrs-0354/keep/share.pdf selected in Finder; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0354/keep/share.pdf.

**Required tools:** `list_dir`, `find_files`, `create_folder`, `reveal_in_finder`, `tag_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `create_folder`; `create_folder` before `reveal_in_finder`; `reveal_in_finder` before `tag_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0354; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0354; Spotlight search exact path; empty folder exact path; reveal only color none; exact path Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0354/Route proposal.txt exists in synthetic Spotlight results
- Target absent; synthetic filesystem only
- Synthetic existing file and native reveal response
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0355 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0355, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0355; then move /tmp/wisp-routing-fixtures/wrs-0355/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0355/output/receipt.txt; then permanently delete only /tmp/wisp-routing-fixtures/wrs-0355/input/disposable.tmp; then show /tmp/wisp-routing-fixtures/wrs-0355/keep/share.pdf selected in Finder.

**Required tools:** `list_dir`, `find_files`, `move_path`, `delete_path`, `reveal_in_finder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `move_path`; `move_path` before `delete_path`; `delete_path` before `reveal_in_finder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0355; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0355; Spotlight search exact source/destination; no overwrite/delete exact path; file only; no directory deletion exact path; reveal only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0355/Route proposal.txt exists in synthetic Spotlight results
- Source exists, destination absent; fixture directories only
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic existing file and native reveal response

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0356 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0356, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0356; then show /tmp/wisp-routing-fixtures/wrs-0356/keep/share.pdf selected in Finder; then zip the folder /tmp/wisp-routing-fixtures/wrs-0356/keep into /tmp/wisp-routing-fixtures/wrs-0356/output/keep.zip; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0356/output/status.txt.

**Required tools:** `list_dir`, `find_files`, `reveal_in_finder`, `archive_files`, `write_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `reveal_in_finder`; `reveal_in_finder` before `archive_files`; `archive_files` before `write_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0356; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0356; Spotlight search exact path; reveal only paths contains keep; archive_path exact; leave inputs exact path/content; text file; not Notes Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0356/Route proposal.txt exists in synthetic Spotlight results
- Synthetic existing file and native reveal response
- Synthetic folder and absent output archive
- Synthetic output directory writable; file absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0357 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0357, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0357; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0357/keep/share.pdf; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0357/input into /tmp/wisp-routing-fixtures/wrs-0357/images; I authorize that scoped move; then move /tmp/wisp-routing-fixtures/wrs-0357/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0357/output/receipt.txt.

**Required tools:** `list_dir`, `find_files`, `tag_file`, `organize_files`, `move_path`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `tag_file`; `tag_file` before `organize_files`; `organize_files` before `move_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0357; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0357; Spotlight search color none; exact path pattern *.png; preview then confirm true; skip collisions; do not move PDF exact source/destination; no overwrite/delete Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0357/Route proposal.txt exists in synthetic Spotlight results
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- Source exists, destination absent; fixture directories only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0358 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0358, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0358; then move /tmp/wisp-routing-fixtures/wrs-0358/input/obsolete-installer.pkg to Trash; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0358/empty-receipts; then convert /tmp/wisp-routing-fixtures/wrs-0358/input/sample.png to JPEG.

**Required tools:** `list_dir`, `find_files`, `trash_file`, `create_folder`, `convert_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `trash_file`; `trash_file` before `create_folder`; `create_folder` before `convert_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0358; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0358; Spotlight search exact path; recoverable removal, not permanent deletion exact path; empty folder to_format jpeg; correct source; no OCR Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0358/Route proposal.txt exists in synthetic Spotlight results
- Disposable fixture item; Trash operation intercepted
- Target absent; synthetic filesystem only
- Synthetic PNG; expected new image path absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0359 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0359, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0359; then move /tmp/wisp-routing-fixtures/wrs-0359/input/obsolete-installer.pkg to Trash; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0359/keep/share.pdf; then show /tmp/wisp-routing-fixtures/wrs-0359/keep/share.pdf selected in Finder.

**Required tools:** `list_dir`, `find_files`, `trash_file`, `tag_file`, `reveal_in_finder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `trash_file`; `trash_file` before `tag_file`; `tag_file` before `reveal_in_finder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0359; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0359; Spotlight search exact path; recoverable removal, not permanent deletion color none; exact path exact path; reveal only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0359/Route proposal.txt exists in synthetic Spotlight results
- Disposable fixture item; Trash operation intercepted
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path
- Synthetic existing file and native reveal response

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0360 · Explicit sequence

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Please do these in this order: list /tmp/wisp-routing-fixtures/wrs-0360, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0360; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0360/output/status.txt; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0360/keep at /tmp/wisp-routing-fixtures/wrs-0360/backup-copy; then move /tmp/wisp-routing-fixtures/wrs-0360/input/obsolete-installer.pkg to Trash.

**Required tools:** `list_dir`, `find_files`, `write_file`, `backup_folder`, `trash_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `write_file`; `write_file` before `backup_folder`; `backup_folder` before `trash_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0360; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0360; Spotlight search exact path/content; text file; not Notes exact source/destination; preserve source exact path; recoverable removal, not permanent deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0360/Route proposal.txt exists in synthetic Spotlight results
- Synthetic output directory writable; file absent
- Destination absent; isolated filesystem fixture
- Disposable fixture item; Trash operation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0361 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0361, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0361. Help me AirDrop /tmp/wisp-routing-fixtures/wrs-0361/keep/share.pdf. Create an empty folder at /tmp/wisp-routing-fixtures/wrs-0361/empty-receipts. Move /tmp/wisp-routing-fixtures/wrs-0361/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0361/output/receipt.txt. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `airdrop_file`, `create_folder`, `move_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0361; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0361; Spotlight search exact file; manual Finder/share handoff only exact path; empty folder exact source/destination; no overwrite/delete Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0361/Route proposal.txt exists in synthetic Spotlight results
- File exists; native reveal/share path intercepted
- Target absent; synthetic filesystem only
- Source exists, destination absent; fixture directories only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0362 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0362, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0362. Make a full backup of /tmp/wisp-routing-fixtures/wrs-0362/keep at /tmp/wisp-routing-fixtures/wrs-0362/backup-copy. Remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0362/keep/share.pdf. Show /tmp/wisp-routing-fixtures/wrs-0362/keep/share.pdf selected in Finder. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `backup_folder`, `tag_file`, `reveal_in_finder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0362; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0362; Spotlight search exact source/destination; preserve source color none; exact path exact path; reveal only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0362/Route proposal.txt exists in synthetic Spotlight results
- Destination absent; isolated filesystem fixture
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path
- Synthetic existing file and native reveal response

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0363 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0363, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0363. Convert /tmp/wisp-routing-fixtures/wrs-0363/input/sample.rtf to a Word docx file. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0363/input/disposable.tmp. Zip the folder /tmp/wisp-routing-fixtures/wrs-0363/keep into /tmp/wisp-routing-fixtures/wrs-0363/output/keep.zip. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `convert_file`, `delete_path`, `archive_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0363; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0363; Spotlight search to_format docx; preserve input; new output exact path; file only; no directory deletion paths contains keep; archive_path exact; leave inputs Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0363/Route proposal.txt exists in synthetic Spotlight results
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0364 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0364, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0364. Convert /tmp/wisp-routing-fixtures/wrs-0364/input/sample.rtf to a Word docx file. Move /tmp/wisp-routing-fixtures/wrs-0364/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0364/output/receipt.txt. Remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0364/keep/share.pdf. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `convert_file`, `move_path`, `tag_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0364; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0364; Spotlight search to_format docx; preserve input; new output exact source/destination; no overwrite/delete color none; exact path Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0364/Route proposal.txt exists in synthetic Spotlight results
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Source exists, destination absent; fixture directories only
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0365 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0365, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0365. Convert /tmp/wisp-routing-fixtures/wrs-0365/input/sample.rtf to a Word docx file. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0365/output/status.txt. Help me AirDrop /tmp/wisp-routing-fixtures/wrs-0365/keep/share.pdf. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `convert_file`, `write_file`, `airdrop_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0365; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0365; Spotlight search to_format docx; preserve input; new output exact path/content; text file; not Notes exact file; manual Finder/share handoff only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0365/Route proposal.txt exists in synthetic Spotlight results
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic output directory writable; file absent
- File exists; native reveal/share path intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0366 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0366, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0366. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0366/input/disposable.tmp. Give /tmp/wisp-routing-fixtures/wrs-0366/keep/share.pdf a blue Finder label. Show /tmp/wisp-routing-fixtures/wrs-0366/keep/share.pdf selected in Finder. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `delete_path`, `tag_file`, `reveal_in_finder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0366; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0366; Spotlight search exact path; file only; no directory deletion color blue; exact path exact path; reveal only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0366/Route proposal.txt exists in synthetic Spotlight results
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic Finder metadata; no actual tags changed
- Synthetic existing file and native reveal response

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0367 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0367, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0367. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0367/input/disposable.tmp. Move /tmp/wisp-routing-fixtures/wrs-0367/input/obsolete-installer.pkg to Trash. Zip the folder /tmp/wisp-routing-fixtures/wrs-0367/keep into /tmp/wisp-routing-fixtures/wrs-0367/output/keep.zip. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `delete_path`, `trash_file`, `archive_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0367; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0367; Spotlight search exact path; file only; no directory deletion exact path; recoverable removal, not permanent deletion paths contains keep; archive_path exact; leave inputs Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0367/Route proposal.txt exists in synthetic Spotlight results
- Disposable synthetic file, explicit permanent deletion intent
- Disposable fixture item; Trash operation intercepted
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0368 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0368, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0368. Move /tmp/wisp-routing-fixtures/wrs-0368/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0368/output/receipt.txt. Convert /tmp/wisp-routing-fixtures/wrs-0368/input/sample.rtf to a Word docx file. Make a full backup of /tmp/wisp-routing-fixtures/wrs-0368/keep at /tmp/wisp-routing-fixtures/wrs-0368/backup-copy. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `move_path`, `convert_file`, `backup_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0368; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0368; Spotlight search exact source/destination; no overwrite/delete to_format docx; preserve input; new output exact source/destination; preserve source Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0368/Route proposal.txt exists in synthetic Spotlight results
- Source exists, destination absent; fixture directories only
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Destination absent; isolated filesystem fixture

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0369 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0369, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0369. Move /tmp/wisp-routing-fixtures/wrs-0369/input/obsolete-installer.pkg to Trash. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0369/input/disposable.tmp. Show /tmp/wisp-routing-fixtures/wrs-0369/keep/share.pdf selected in Finder. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `trash_file`, `delete_path`, `reveal_in_finder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0369; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0369; Spotlight search exact path; recoverable removal, not permanent deletion exact path; file only; no directory deletion exact path; reveal only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0369/Route proposal.txt exists in synthetic Spotlight results
- Disposable fixture item; Trash operation intercepted
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic existing file and native reveal response

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0370 · Natural compound request

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

I have a few things to finish. List /tmp/wisp-routing-fixtures/wrs-0370, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0370. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0370/output/status.txt. Create an empty folder at /tmp/wisp-routing-fixtures/wrs-0370/empty-receipts. Move /tmp/wisp-routing-fixtures/wrs-0370/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0370/output/receipt.txt. Keep the results separate so I can tell what came from where.

**Required tools:** `list_dir`, `find_files`, `write_file`, `create_folder`, `move_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0370; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0370; Spotlight search exact path/content; text file; not Notes exact path; empty folder exact source/destination; no overwrite/delete Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0370/Route proposal.txt exists in synthetic Spotlight results
- Synthetic output directory writable; file absent
- Target absent; synthetic filesystem only
- Source exists, destination absent; fixture directories only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0371 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0371, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0371; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0371/keep/share.pdf; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0371/keep/share.pdf; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0371/empty-receipts. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `airdrop_file`, `tag_file`, `create_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `airdrop_file`; `airdrop_file` before `tag_file`; `tag_file` before `create_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0371; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0371; Spotlight search exact file; manual Finder/share handoff only color none; exact path exact path; empty folder Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0371/Route proposal.txt exists in synthetic Spotlight results
- File exists; native reveal/share path intercepted
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path
- Target absent; synthetic filesystem only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0372 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0372, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0372; then zip the folder /tmp/wisp-routing-fixtures/wrs-0372/keep into /tmp/wisp-routing-fixtures/wrs-0372/output/keep.zip; then move /tmp/wisp-routing-fixtures/wrs-0372/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0372/output/receipt.txt; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0372/input into /tmp/wisp-routing-fixtures/wrs-0372/images; I authorize that scoped move. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `archive_files`, `move_path`, `organize_files`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `archive_files`; `archive_files` before `move_path`; `move_path` before `organize_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0372; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0372; Spotlight search paths contains keep; archive_path exact; leave inputs exact source/destination; no overwrite/delete pattern *.png; preview then confirm true; skip collisions; do not move PDF Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0372/Route proposal.txt exists in synthetic Spotlight results
- Synthetic folder and absent output archive
- Source exists, destination absent; fixture directories only
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0373 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0373, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0373; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0373/keep at /tmp/wisp-routing-fixtures/wrs-0373/backup-copy; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0373/keep/share.pdf; then zip the folder /tmp/wisp-routing-fixtures/wrs-0373/keep into /tmp/wisp-routing-fixtures/wrs-0373/output/keep.zip. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `backup_folder`, `airdrop_file`, `archive_files`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `backup_folder`; `backup_folder` before `airdrop_file`; `airdrop_file` before `archive_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0373; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0373; Spotlight search exact source/destination; preserve source exact file; manual Finder/share handoff only paths contains keep; archive_path exact; leave inputs Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0373/Route proposal.txt exists in synthetic Spotlight results
- Destination absent; isolated filesystem fixture
- File exists; native reveal/share path intercepted
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0374 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0374, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0374; then convert /tmp/wisp-routing-fixtures/wrs-0374/input/sample.rtf to a Word docx file; then zip the folder /tmp/wisp-routing-fixtures/wrs-0374/keep into /tmp/wisp-routing-fixtures/wrs-0374/output/keep.zip; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0374/keep at /tmp/wisp-routing-fixtures/wrs-0374/backup-copy. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `convert_file`, `archive_files`, `backup_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `convert_file`; `convert_file` before `archive_files`; `archive_files` before `backup_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0374; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0374; Spotlight search to_format docx; preserve input; new output paths contains keep; archive_path exact; leave inputs exact source/destination; preserve source Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0374/Route proposal.txt exists in synthetic Spotlight results
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic folder and absent output archive
- Destination absent; isolated filesystem fixture

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0375 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0375, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0375; then convert /tmp/wisp-routing-fixtures/wrs-0375/input/sample.rtf to a Word docx file; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0375/output/status.txt; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0375/empty-receipts. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `convert_file`, `write_file`, `create_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `convert_file`; `convert_file` before `write_file`; `write_file` before `create_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0375; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0375; Spotlight search to_format docx; preserve input; new output exact path/content; text file; not Notes exact path; empty folder Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0375/Route proposal.txt exists in synthetic Spotlight results
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic output directory writable; file absent
- Target absent; synthetic filesystem only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0376 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0376, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0376; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0376/empty-receipts; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0376/keep/share.pdf; then zip the folder /tmp/wisp-routing-fixtures/wrs-0376/keep into /tmp/wisp-routing-fixtures/wrs-0376/output/keep.zip. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `create_folder`, `airdrop_file`, `archive_files`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `create_folder`; `create_folder` before `airdrop_file`; `airdrop_file` before `archive_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0376; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0376; Spotlight search exact path; empty folder exact file; manual Finder/share handoff only paths contains keep; archive_path exact; leave inputs Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0376/Route proposal.txt exists in synthetic Spotlight results
- Target absent; synthetic filesystem only
- File exists; native reveal/share path intercepted
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0377 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0377, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0377; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0377/empty-receipts; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0377/keep at /tmp/wisp-routing-fixtures/wrs-0377/backup-copy; then zip the folder /tmp/wisp-routing-fixtures/wrs-0377/keep into /tmp/wisp-routing-fixtures/wrs-0377/output/keep.zip. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `create_folder`, `backup_folder`, `archive_files`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `create_folder`; `create_folder` before `backup_folder`; `backup_folder` before `archive_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0377; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0377; Spotlight search exact path; empty folder exact source/destination; preserve source paths contains keep; archive_path exact; leave inputs Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0377/Route proposal.txt exists in synthetic Spotlight results
- Target absent; synthetic filesystem only
- Destination absent; isolated filesystem fixture
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0378 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0378, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0378; then permanently delete only /tmp/wisp-routing-fixtures/wrs-0378/input/disposable.tmp; then move /tmp/wisp-routing-fixtures/wrs-0378/input/obsolete-installer.pkg to Trash; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0378/output/status.txt. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `delete_path`, `trash_file`, `write_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `delete_path`; `delete_path` before `trash_file`; `trash_file` before `write_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0378; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0378; Spotlight search exact path; file only; no directory deletion exact path; recoverable removal, not permanent deletion exact path/content; text file; not Notes Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0378/Route proposal.txt exists in synthetic Spotlight results
- Disposable synthetic file, explicit permanent deletion intent
- Disposable fixture item; Trash operation intercepted
- Synthetic output directory writable; file absent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0379 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0379, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0379; then move /tmp/wisp-routing-fixtures/wrs-0379/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0379/output/receipt.txt; then convert /tmp/wisp-routing-fixtures/wrs-0379/input/sample.rtf to a Word docx file; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0379/empty-receipts. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `move_path`, `convert_file`, `create_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `move_path`; `move_path` before `convert_file`; `convert_file` before `create_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0379; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0379; Spotlight search exact source/destination; no overwrite/delete to_format docx; preserve input; new output exact path; empty folder Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0379/Route proposal.txt exists in synthetic Spotlight results
- Source exists, destination absent; fixture directories only
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Target absent; synthetic filesystem only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0380 · Scoped execution

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

For these tasks, use only the named sources and targets: list /tmp/wisp-routing-fixtures/wrs-0380, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0380; then move /tmp/wisp-routing-fixtures/wrs-0380/input/obsolete-installer.pkg to Trash; then show /tmp/wisp-routing-fixtures/wrs-0380/keep/share.pdf selected in Finder; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0380/keep at /tmp/wisp-routing-fixtures/wrs-0380/backup-copy. Leave everything else unchanged.

**Required tools:** `list_dir`, `find_files`, `trash_file`, `reveal_in_finder`, `backup_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `trash_file`; `trash_file` before `reveal_in_finder`; `reveal_in_finder` before `backup_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0380; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0380; Spotlight search exact path; recoverable removal, not permanent deletion exact path; reveal only exact source/destination; preserve source Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0380/Route proposal.txt exists in synthetic Spotlight results
- Disposable fixture item; Trash operation intercepted
- Synthetic existing file and native reveal response
- Destination absent; isolated filesystem fixture

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0381 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0381, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0381. Help me AirDrop /tmp/wisp-routing-fixtures/wrs-0381/keep/share.pdf. Show /tmp/wisp-routing-fixtures/wrs-0381/keep/share.pdf selected in Finder. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0381/input/disposable.tmp. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `airdrop_file`, `reveal_in_finder`, `delete_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0381; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0381; Spotlight search exact file; manual Finder/share handoff only exact path; reveal only exact path; file only; no directory deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0381/Route proposal.txt exists in synthetic Spotlight results
- File exists; native reveal/share path intercepted
- Synthetic existing file and native reveal response
- Disposable synthetic file, explicit permanent deletion intent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0382 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0382, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0382. Create an empty folder at /tmp/wisp-routing-fixtures/wrs-0382/empty-receipts. Make a full backup of /tmp/wisp-routing-fixtures/wrs-0382/keep at /tmp/wisp-routing-fixtures/wrs-0382/backup-copy. Help me AirDrop /tmp/wisp-routing-fixtures/wrs-0382/keep/share.pdf. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `create_folder`, `backup_folder`, `airdrop_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0382; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0382; Spotlight search exact path; empty folder exact source/destination; preserve source exact file; manual Finder/share handoff only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0382/Route proposal.txt exists in synthetic Spotlight results
- Target absent; synthetic filesystem only
- Destination absent; isolated filesystem fixture
- File exists; native reveal/share path intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0383 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0383, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0383. Create an empty folder at /tmp/wisp-routing-fixtures/wrs-0383/empty-receipts. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0383/output/status.txt. Show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0383/input into /tmp/wisp-routing-fixtures/wrs-0383/images; I authorize that scoped move. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `create_folder`, `write_file`, `organize_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0383; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0383; Spotlight search exact path; empty folder exact path/content; text file; not Notes pattern *.png; preview then confirm true; skip collisions; do not move PDF Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0383/Route proposal.txt exists in synthetic Spotlight results
- Target absent; synthetic filesystem only
- Synthetic output directory writable; file absent
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0384 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0384, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0384. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0384/input/disposable.tmp. Zip the folder /tmp/wisp-routing-fixtures/wrs-0384/keep into /tmp/wisp-routing-fixtures/wrs-0384/output/keep.zip. Move /tmp/wisp-routing-fixtures/wrs-0384/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0384/output/receipt.txt. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `delete_path`, `archive_files`, `move_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0384; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0384; Spotlight search exact path; file only; no directory deletion paths contains keep; archive_path exact; leave inputs exact source/destination; no overwrite/delete Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0384/Route proposal.txt exists in synthetic Spotlight results
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic folder and absent output archive
- Source exists, destination absent; fixture directories only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0385 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0385, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0385. Show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0385/input into /tmp/wisp-routing-fixtures/wrs-0385/images; I authorize that scoped move. Help me AirDrop /tmp/wisp-routing-fixtures/wrs-0385/keep/share.pdf. Show /tmp/wisp-routing-fixtures/wrs-0385/keep/share.pdf selected in Finder. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `organize_files`, `airdrop_file`, `reveal_in_finder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0385; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0385; Spotlight search pattern *.png; preview then confirm true; skip collisions; do not move PDF exact file; manual Finder/share handoff only exact path; reveal only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0385/Route proposal.txt exists in synthetic Spotlight results
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- File exists; native reveal/share path intercepted
- Synthetic existing file and native reveal response

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0386 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0386, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0386. Show /tmp/wisp-routing-fixtures/wrs-0386/keep/share.pdf selected in Finder. Show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0386/input into /tmp/wisp-routing-fixtures/wrs-0386/images; I authorize that scoped move. Move /tmp/wisp-routing-fixtures/wrs-0386/input/obsolete-installer.pkg to Trash. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `reveal_in_finder`, `organize_files`, `trash_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0386; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0386; Spotlight search exact path; reveal only pattern *.png; preview then confirm true; skip collisions; do not move PDF exact path; recoverable removal, not permanent deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0386/Route proposal.txt exists in synthetic Spotlight results
- Synthetic existing file and native reveal response
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- Disposable fixture item; Trash operation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0387 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0387, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0387. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0387/output/status.txt. Convert /tmp/wisp-routing-fixtures/wrs-0387/input/sample.rtf to a Word docx file. Help me AirDrop /tmp/wisp-routing-fixtures/wrs-0387/keep/share.pdf. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `write_file`, `convert_file`, `airdrop_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0387; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0387; Spotlight search exact path/content; text file; not Notes to_format docx; preserve input; new output exact file; manual Finder/share handoff only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0387/Route proposal.txt exists in synthetic Spotlight results
- Synthetic output directory writable; file absent
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- File exists; native reveal/share path intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0388 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0388, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0388. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0388/output/status.txt. Convert /tmp/wisp-routing-fixtures/wrs-0388/input/sample.rtf to a Word docx file. Zip the folder /tmp/wisp-routing-fixtures/wrs-0388/keep into /tmp/wisp-routing-fixtures/wrs-0388/output/keep.zip. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `write_file`, `convert_file`, `archive_files`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0388; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0388; Spotlight search exact path/content; text file; not Notes to_format docx; preserve input; new output paths contains keep; archive_path exact; leave inputs Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0388/Route proposal.txt exists in synthetic Spotlight results
- Synthetic output directory writable; file absent
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output
- Synthetic folder and absent output archive

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0389 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0389, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0389. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0389/output/status.txt. Permanently delete only /tmp/wisp-routing-fixtures/wrs-0389/input/disposable.tmp. Remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0389/keep/share.pdf. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `write_file`, `delete_path`, `tag_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0389; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0389; Spotlight search exact path/content; text file; not Notes exact path; file only; no directory deletion color none; exact path Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0389/Route proposal.txt exists in synthetic Spotlight results
- Synthetic output directory writable; file absent
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0390 · Late constraints

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

List /tmp/wisp-routing-fixtures/wrs-0390, including its subfolders two levels deep. Find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0390. Save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0390/output/status.txt. Preview moving only *.png files from /tmp/wisp-routing-fixtures/wrs-0390/input to /tmp/wisp-routing-fixtures/wrs-0390/images, but do not move them yet. Move /tmp/wisp-routing-fixtures/wrs-0390/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0390/output/receipt.txt. One last constraint: do not guess a missing value or silently substitute a different app, account, or destination.

**Required tools:** `list_dir`, `find_files`, `write_file`, `organize_files`, `move_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0390; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0390; Spotlight search exact path/content; text file; not Notes pattern *.png; confirm false; files only exact source/destination; no overwrite/delete Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0390/Route proposal.txt exists in synthetic Spotlight results
- Synthetic output directory writable; file absent
- Two PNGs and one PDF in input; no mutations for preview
- Source exists, destination absent; fixture directories only

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0391 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0391, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0391; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0391/keep/share.pdf; then zip the folder /tmp/wisp-routing-fixtures/wrs-0391/keep into /tmp/wisp-routing-fixtures/wrs-0391/output/keep.zip; then permanently delete only /tmp/wisp-routing-fixtures/wrs-0391/input/disposable.tmp? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `airdrop_file`, `archive_files`, `delete_path`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `airdrop_file`; `airdrop_file` before `archive_files`; `archive_files` before `delete_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0391; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0391; Spotlight search exact file; manual Finder/share handoff only paths contains keep; archive_path exact; leave inputs exact path; file only; no directory deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0391/Route proposal.txt exists in synthetic Spotlight results
- File exists; native reveal/share path intercepted
- Synthetic folder and absent output archive
- Disposable synthetic file, explicit permanent deletion intent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0392 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0392, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0392; then zip the folder /tmp/wisp-routing-fixtures/wrs-0392/keep into /tmp/wisp-routing-fixtures/wrs-0392/output/keep.zip; then permanently delete only /tmp/wisp-routing-fixtures/wrs-0392/input/disposable.tmp; then give /tmp/wisp-routing-fixtures/wrs-0392/keep/share.pdf a blue Finder label? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `archive_files`, `delete_path`, `tag_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `archive_files`; `archive_files` before `delete_path`; `delete_path` before `tag_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0392; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0392; Spotlight search paths contains keep; archive_path exact; leave inputs exact path; file only; no directory deletion color blue; exact path Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0392/Route proposal.txt exists in synthetic Spotlight results
- Synthetic folder and absent output archive
- Disposable synthetic file, explicit permanent deletion intent
- Synthetic Finder metadata; no actual tags changed

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0393 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0393, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0393; then zip the folder /tmp/wisp-routing-fixtures/wrs-0393/keep into /tmp/wisp-routing-fixtures/wrs-0393/output/keep.zip; then show /tmp/wisp-routing-fixtures/wrs-0393/keep/share.pdf selected in Finder; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0393/keep/share.pdf? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `archive_files`, `reveal_in_finder`, `tag_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `archive_files`; `archive_files` before `reveal_in_finder`; `reveal_in_finder` before `tag_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0393; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0393; Spotlight search paths contains keep; archive_path exact; leave inputs exact path; reveal only color none; exact path Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0393/Route proposal.txt exists in synthetic Spotlight results
- Synthetic folder and absent output archive
- Synthetic existing file and native reveal response
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0394 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0394, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0394; then permanently delete only /tmp/wisp-routing-fixtures/wrs-0394/input/disposable.tmp; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0394/keep at /tmp/wisp-routing-fixtures/wrs-0394/backup-copy; then move /tmp/wisp-routing-fixtures/wrs-0394/input/obsolete-installer.pkg to Trash? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `delete_path`, `backup_folder`, `trash_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `delete_path`; `delete_path` before `backup_folder`; `backup_folder` before `trash_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0394; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0394; Spotlight search exact path; file only; no directory deletion exact source/destination; preserve source exact path; recoverable removal, not permanent deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0394/Route proposal.txt exists in synthetic Spotlight results
- Disposable synthetic file, explicit permanent deletion intent
- Destination absent; isolated filesystem fixture
- Disposable fixture item; Trash operation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0395 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0395, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0395; then move /tmp/wisp-routing-fixtures/wrs-0395/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0395/output/receipt.txt; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0395/keep/share.pdf; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0395/keep/share.pdf? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `move_path`, `airdrop_file`, `tag_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `move_path`; `move_path` before `airdrop_file`; `airdrop_file` before `tag_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0395; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0395; Spotlight search exact source/destination; no overwrite/delete exact file; manual Finder/share handoff only color none; exact path Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0395/Route proposal.txt exists in synthetic Spotlight results
- Source exists, destination absent; fixture directories only
- File exists; native reveal/share path intercepted
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0396 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0396, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0396; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0396/input into /tmp/wisp-routing-fixtures/wrs-0396/images; I authorize that scoped move; then move /tmp/wisp-routing-fixtures/wrs-0396/input/receipt.txt to /tmp/wisp-routing-fixtures/wrs-0396/output/receipt.txt; then help me AirDrop /tmp/wisp-routing-fixtures/wrs-0396/keep/share.pdf? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `organize_files`, `move_path`, `airdrop_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `organize_files`; `organize_files` before `move_path`; `move_path` before `airdrop_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0396; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0396; Spotlight search pattern *.png; preview then confirm true; skip collisions; do not move PDF exact source/destination; no overwrite/delete exact file; manual Finder/share handoff only Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0396/Route proposal.txt exists in synthetic Spotlight results
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- Source exists, destination absent; fixture directories only
- File exists; native reveal/share path intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0397 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0397, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0397; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0397/input into /tmp/wisp-routing-fixtures/wrs-0397/images; I authorize that scoped move; then move /tmp/wisp-routing-fixtures/wrs-0397/input/obsolete-installer.pkg to Trash; then convert /tmp/wisp-routing-fixtures/wrs-0397/input/sample.rtf to a Word docx file? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `organize_files`, `trash_file`, `convert_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `organize_files`; `organize_files` before `trash_file`; `trash_file` before `convert_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0397; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0397; Spotlight search pattern *.png; preview then confirm true; skip collisions; do not move PDF exact path; recoverable removal, not permanent deletion to_format docx; preserve input; new output Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0397/Route proposal.txt exists in synthetic Spotlight results
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- Disposable fixture item; Trash operation intercepted
- Synthetic PNG; expected new image path absent; variant-specific state must satisfy: to_format docx; preserve input; new output

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0398 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0398, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0398; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0398/input into /tmp/wisp-routing-fixtures/wrs-0398/images; I authorize that scoped move; then save "Route fixture complete." as /tmp/wisp-routing-fixtures/wrs-0398/output/status.txt; then move /tmp/wisp-routing-fixtures/wrs-0398/input/obsolete-installer.pkg to Trash? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `organize_files`, `write_file`, `trash_file`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `organize_files`; `organize_files` before `write_file`; `write_file` before `trash_file`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0398; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0398; Spotlight search pattern *.png; preview then confirm true; skip collisions; do not move PDF exact path/content; text file; not Notes exact path; recoverable removal, not permanent deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0398/Route proposal.txt exists in synthetic Spotlight results
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- Synthetic output directory writable; file absent
- Disposable fixture item; Trash operation intercepted

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0399 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0399, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0399; then remove the Finder color label from /tmp/wisp-routing-fixtures/wrs-0399/keep/share.pdf; then create an empty folder at /tmp/wisp-routing-fixtures/wrs-0399/empty-receipts; then permanently delete only /tmp/wisp-routing-fixtures/wrs-0399/input/disposable.tmp? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `tag_file`, `create_folder`, `delete_path`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `tag_file`; `tag_file` before `create_folder`; `create_folder` before `delete_path`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0399; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0399; Spotlight search color none; exact path exact path; empty folder exact path; file only; no directory deletion Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0399/Route proposal.txt exists in synthetic Spotlight results
- Synthetic Finder metadata; no actual tags changed; variant-specific state must satisfy: color none; exact path
- Target absent; synthetic filesystem only
- Disposable synthetic file, explicit permanent deletion intent

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.

### WRS-0400 · Colloquial with interruptions

**Focus:** Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.

**Prompt:**

Could you list /tmp/wisp-routing-fixtures/wrs-0400, including its subfolders two levels deep; then find files named Route proposal under /tmp/wisp-routing-fixtures/wrs-0400; then move /tmp/wisp-routing-fixtures/wrs-0400/input/obsolete-installer.pkg to Trash; then show the PNG-move preview, then move only those PNG files from /tmp/wisp-routing-fixtures/wrs-0400/input into /tmp/wisp-routing-fixtures/wrs-0400/images; I authorize that scoped move; then make a full backup of /tmp/wisp-routing-fixtures/wrs-0400/keep at /tmp/wisp-routing-fixtures/wrs-0400/backup-copy? I mean the actual items named here, even where the wording sounds like another app. If one part is unavailable, say which and continue the independent parts.

**Required tools:** `list_dir`, `find_files`, `trash_file`, `organize_files`, `backup_folder`.
**Ordering constraints:** `list_dir` before `find_files`; `find_files` before `trash_file`; `trash_file` before `organize_files`; `organize_files` before `backup_folder`.
**Checks:** path /tmp/wisp-routing-fixtures/wrs-0400; recursive true; max_depth 2 query Route proposal; folder /tmp/wisp-routing-fixtures/wrs-0400; Spotlight search exact path; recoverable removal, not permanent deletion pattern *.png; preview then confirm true; skip collisions; do not move PDF exact source/destination; preserve source Inspect targets, keep changes scoped, prefer recoverable deletion, and verify outputs.
**Forbidden:** `create_tool`, `run_applescript`, `run_shell`.

**Synthetic fixture requirements:**

- Synthetic directory tree has input, output, and keep folders
- /tmp/wisp-routing-fixtures/wrs-0400/Route proposal.txt exists in synthetic Spotlight results
- Disposable fixture item; Trash operation intercepted
- Two PNGs and one PDF in input; no mutations for preview; variant-specific state must satisfy: pattern *.png; preview then confirm true; skip collisions; do not move PDF
- Destination absent; isolated filesystem fixture

Status: **NOT RUN**. Execution, if later authorized, must use intercepted synthetic fixtures.
