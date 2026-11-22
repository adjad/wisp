---
name: file-tidy
description: Plans how to tidy a folder (Downloads by default) by file type and age, shows the planned moves, and moves nothing until the person confirms. Never deletes. Use for "tidy my Downloads", "sort my desktop files", "organize this folder".
metadata:
  wisp:
    triggers:
      - tidy my downloads
      - tidy my desktop
      - tidy my documents
      - tidy this folder
      - clean up my downloads
      - clean up my desktop
      - file tidy
      - sort my downloads
      - organize my downloads
    enabled: true
    contract:
      read_only: false
      grants: none
      folders: ["~/Downloads", "~/Desktop", "~/Documents", "~/Pictures", "~/Movies", "~/Music"]
      reads: [list_dir]
      preview: [organize_files]
      confirms: [organize_files]
      steps:
        - {tool: list_dir, args: {path: "~/Downloads", details: true, sort: modified}}
        - {tool: organize_files, args: {pattern: "*", folder: "~/Downloads", destination: ".", group_by: extension, confirm: false}}
        - {tool: organize_files, args: {pattern: "*", folder: "~/Downloads", destination: ".", group_by: extension, confirm: true, preview_token: from-the-preview}, confirm: plan}
---

# File tidy

A fixed procedure. You fill in the folder and the file pattern; the steps never
change. Nothing moves until the person has seen the plan and said yes. This
skill never deletes, never empties the Trash, and never moves a file outside the
person's own folders (Downloads, Desktop, Documents, Pictures, Movies, Music).

## 1. Choose the folder

Use the folder the person named. If they named none, use `~/Downloads`. If
they named something outside those folders, or their whole home folder, a
system folder or a cloud-sync root, say it is not covered and offer Downloads
or Desktop instead.

## 2. Look (read only)

Call `list_dir` with `details: true` and `sort: "modified"` for the folder.
Sort what you see into types (images, PDFs and documents, archives, installers,
movies and audio, other) and ages (this month, 1 to 6 months, older). Call out
anything that looks in use: items modified in the last day, folders, and files
that are still downloading (`.download`, `.crdownload`, `.part`). Leave those
out of the plan.

## 3. Show the plan (nothing moves)

Call `organize_files` with `confirm: false`, `group_by: "extension"` and
`destination: "."` (the folders are created inside the source folder), once
per file pattern that makes sense (for example `*.pdf`, `*.png`). The default
preview changes nothing and lists every move from source to target with a
preview token.

Then present one plan in plain words: for each type, how many files, from where
and to which folder, and the age breakdown (for example "9 PDFs, 6 older than
three months"). Say what is left untouched and why. Age is for your summary
only; the tool groups by extension, so do not promise age-named folders.

Ask: "Move these N files as shown?" and stop. Do not move anything in the same
reply as the plan.

## 4. Move only after a yes

Only after an explicit yes to that exact plan, call `organize_files` again with
the same `pattern`, `folder`, `destination` and `group_by`, plus `confirm: true`
and that plan's `preview_token`. Wisp shows its own confirmation card for the
call; do not try to avoid it. A different or edited plan needs a new preview and
a new yes. A yes for one pattern covers only that pattern. Never ask for a
standing or "always" approval.

If the preview expired or the call reports an error or unknown result, say so
and do not retry on your own.

## 5. Finish

Report what the tool's receipt says: files moved, files skipped, anything
unknown. Mention that nothing was deleted and that the person can move files
back from the new folders.
