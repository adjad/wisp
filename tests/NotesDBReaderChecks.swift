// Why closed Notes can't be read, and what is read from a store that can be,
// from synthetic fixtures only: never the person's NoteStore. Compile with
// NotesDBReader.swift (and -lsqlite3).
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
        let noteTables = """
            CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT);
            INSERT INTO Z_PRIMARYKEY VALUES (5, 'ICNote');
            CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER, Z_ENT INTEGER, ZTITLE1 TEXT, ZTITLE2 TEXT,
                ZMODIFICATIONDATE1 REAL, ZFOLDER INTEGER, ZMARKEDFORDELETION INTEGER);
            CREATE TABLE ZICNOTEDATA (ZNOTE INTEGER, ZDATA BLOB);

            """
        let emptyPath = root.appendingPathComponent("Empty.sqlite").path
        check(sqlite3_open(emptyPath, &db) == SQLITE_OK, "fixture open")
        check(sqlite3_exec(db, noteTables, nil, nil, nil) == SQLITE_OK, "fixture")
        sqlite3_close(db)
        let empty = R(storePath: emptyPath)
        check(empty.readNotes() == "" && empty.lastFailure == nil, "an empty readable store is empty")
        // A later failure on the same reader replaces nothing stale.
        check(missing.readNotes() == nil && missing.lastFailure != nil, "each read sets its own failure")

        // A note's text is also re-read as a message. Nine bytes of CJK or
        // Hangul and then an odd ASCII byte look like a length past Int.max
        // there: that is skipped, never followed and never a crash.
        typealias P = ProtobufTextExtractor
        func field(_ body: String) -> Data { Data([0x12, UInt8(body.utf8.count)]) + Data(body.utf8) }
        check(P.longestString(in: Data("b日本語a".utf8)) == nil, "bare text holds no field")
        for body in ["Hi\n你好吗?", "Hi\n고마워!", "b日本語a is how this note starts, and it goes on."] {
            check(P.longestString(in: field(body)) == body, "a body with CJK text is read whole")
        }
        var hugeLength: [UInt8] = [0x0A]
        hugeLength += [UInt8](repeating: 0xFF, count: 9)
        hugeLength.append(0x01)
        check(P.longestString(in: Data(hugeLength)) == nil, "a length past Int.max is not followed")
        check(P.longestString(in: field("Call the plumber on Monday")) == "Call the plumber on Monday",
              "plain text is read as before")

        // Six rows: a folder, two notes to return, and three to skip (no date,
        // no saved text, in Recently Deleted). The skipped ones sit between and
        // after the others, so the read must pass them and still end.
        func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }
        let greeting = "Hi\n你好吗?", plumber = "Call the plumber on Monday"
        let greetingHex = hex(field(greeting)), plumberHex = hex(field(plumber))
        let spareHex = hex(field("This one is left out"))
        let noteRows = """
            INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (10, 3, NULL, 'Home', NULL, NULL, 0);
            INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (1, 5, 'Greeting', NULL, 700000000, 10, 0);
            INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (2, 5, 'No date', NULL, 0, 10, 0);
            INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (3, 5, 'Plumber', NULL, 600000000, NULL, 0);
            INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (4, 5, 'Blank', NULL, 650000000, 10, 0);
            INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (5, 5, 'Binned', NULL, 690000000, 10, 1);
            INSERT INTO ZICNOTEDATA VALUES (1, X'\(greetingHex)');
            INSERT INTO ZICNOTEDATA VALUES (2, X'\(spareHex)');
            INSERT INTO ZICNOTEDATA VALUES (3, X'\(plumberHex)');
            INSERT INTO ZICNOTEDATA VALUES (4, X'');
            INSERT INTO ZICNOTEDATA VALUES (5, X'\(spareHex)');
            """
        let rowsPath = root.appendingPathComponent("Rows.sqlite").path
        check(sqlite3_open(rowsPath, &db) == SQLITE_OK, "fixture open")
        check(sqlite3_exec(db, noteTables + noteRows, nil, nil, nil) == SQLITE_OK, "fixture")
        sqlite3_close(db)
        let rows = R(storePath: rowsPath)
        let newest: String = "1678307200\u{01}Greeting\u{01}Home\u{01}\(greeting)\u{02}"
        let older: String = "1578307200\u{01}Plumber\u{01}\u{01}\(plumber)\u{02}"
        check(rows.readNotes() == newest + older,
              "the readable notes come back newest first, the skipped ones left out")
        check(rows.lastFailure == nil, "a complete read has no failure")

        // The same store with every page after the first two blanked: it opens
        // and its tables are known, but the read stops before any row. That is
        // a failed read with the plain reason, never an empty or shorter Notes.
        let damagedURL = root.appendingPathComponent("Damaged.sqlite")
        check(sqlite3_open(damagedURL.path, &db) == SQLITE_OK, "fixture open")
        check(sqlite3_exec(db, noteTables + noteRows, nil, nil, nil) == SQLITE_OK, "fixture")
        sqlite3_close(db)
        var bytes = try Data(contentsOf: damagedURL)
        let pageSize = (Int(bytes[16]) << 8) | Int(bytes[17])
        check(pageSize >= 512 && bytes.count > 2 * pageSize, "the fixture has pages past its first two")
        bytes.resetBytes(in: (2 * pageSize)..<bytes.count)
        try bytes.write(to: damagedURL)
        let damaged = R(storePath: damagedURL.path)
        check(damaged.readNotes() == nil, "a read that stops early is not an empty Notes")
        check(damaged.lastFailure?.kind == "" && damaged.lastFailure?.reason.contains("just now") == true,
              "a read that stops early keeps the plain reason")
        print("synthetic Notes store checks passed")
    }
}
