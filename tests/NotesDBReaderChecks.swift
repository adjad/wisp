// Why closed Notes can't be read, from synthetic fixtures only: never the
// person's NoteStore. Compile with NotesDBReader.swift (and -lsqlite3).
import Foundation
import SQLite3

@main struct NotesDBReaderChecks {
    static func check(_ ok: Bool, _ message: String) { precondition(ok, message) }

    static func main() throws {
        typealias R = NotesDBReader
        // Full Disk Access only for macOS's privacy denial.
        let denied = R.failure(openErrno: EPERM)
        check(denied.kind == "full_disk_access" && denied.reason.contains("Full Disk Access"), "EPERM")
        for code in [ENOENT, EACCES, EBUSY, EIO, 0] {
            let other = R.failure(openErrno: code)
            check(other.kind == "" && !other.reason.contains("Full Disk Access"),
                  "errno \(code) keeps a plain reason")
        }

        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("wisp-notes-db-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        // Missing store: plain reason, not Full Disk Access.
        let missing = R(storePath: root.appendingPathComponent("NoteStore.sqlite").path)
        check(missing.readNotes() == nil, "a missing store can't be read")
        check(missing.lastFailure?.kind == "" && missing.lastFailure?.reason.contains("didn't find") == true,
              "a missing store says so plainly")

        // A store whose tables changed: unreadable, never an empty success.
        let changedPath = root.appendingPathComponent("Changed.sqlite").path
        var db: OpaquePointer?
        check(sqlite3_open(changedPath, &db) == SQLITE_OK, "fixture open")
        check(sqlite3_exec(db, "CREATE TABLE SOMETHING_ELSE (x INTEGER)", nil, nil, nil) == SQLITE_OK, "fixture")
        sqlite3_close(db)
        let changed = R(storePath: changedPath)
        check(changed.readNotes() == nil, "an unknown schema is not an empty Notes")
        check(changed.lastFailure?.kind == "" && changed.lastFailure?.reason.contains("can't read") == true,
              "an unknown schema keeps a plain reason")

        // A store with the known entity but no note tables: also unreadable.
        let partialPath = root.appendingPathComponent("Partial.sqlite").path
        check(sqlite3_open(partialPath, &db) == SQLITE_OK, "fixture open")
        check(sqlite3_exec(db, "CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT); "
                           + "INSERT INTO Z_PRIMARYKEY VALUES (5, 'ICNote')", nil, nil, nil) == SQLITE_OK, "fixture")
        sqlite3_close(db)
        let partial = R(storePath: partialPath)
        check(partial.readNotes() == nil && partial.lastFailure?.kind == "", "missing note tables")

        // A readable, empty store is a real empty result with no failure.
        let emptyPath = root.appendingPathComponent("Empty.sqlite").path
        check(sqlite3_open(emptyPath, &db) == SQLITE_OK, "fixture open")
        check(sqlite3_exec(db, """
            CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT);
            INSERT INTO Z_PRIMARYKEY VALUES (5, 'ICNote');
            CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, Z_ENT INTEGER, ZTITLE1 TEXT, ZTITLE2 TEXT,
                ZMODIFICATIONDATE1 REAL, ZFOLDER INTEGER, ZMARKEDFORDELETION INTEGER);
            CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB);
            """, nil, nil, nil) == SQLITE_OK, "fixture")
        sqlite3_close(db)
        let empty = R(storePath: emptyPath)
        check(empty.readNotes() == "" && empty.lastFailure == nil, "an empty readable store is empty")
        // A later failure on the same reader replaces nothing stale.
        check(missing.readNotes() == nil && missing.lastFailure != nil, "each read sets its own failure")
        print("synthetic Notes store checks passed")
    }
}
