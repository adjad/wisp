// Contract for decoding `workspace.proactive` (app/Sources/WispApp/ProactiveModels.swift)
// and its pure presentation rules. Pure logic: never launches Wisp or contacts its service.
import Foundation

@main
struct ProactiveChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }
        func decode(_ json: String) -> ProactiveState? {
            try? JSONDecoder().decode(ProactiveState.self, from: Data(json.utf8))
        }
        struct Workspace: Decodable { var proactive: ProactiveState? = nil }
        func workspace(_ json: String) -> Workspace? {
            try? JSONDecoder().decode(Workspace.self, from: Data(json.utf8))
        }

        // The contract's full shape.
        let full = decode("""
        {"state":"ready","as_of":1700000000,"progress":{"done":12,"total":40},
         "accounts":["me@ucsc.edu","judge@gmail.com"],"summary":"A steady day.",
         "cards":[{"id":"c1","meta":"Overdue · 3 days · Mail","title":"Reply to Dana","body":"I checked…","why":"Asked twice",
                   "account":"me@ucsc.edu","sources":[{"kind":"mail","ref":"m1","who":"Dana","quote":"coffee?"}],
                   "action":{"method":"email_reply","button":"Reply & add to calendar?","draft":"Hi Dana","recipient":"dana@x.com",
                             "event":{"start":1},"envelope":{"opaque":true}}}],
         "todos":[{"id":"t1","title":"Coffee chat","source":"mail","account":"me@ucsc.edu","who":"Dana","quote":"coffee?",
                   "due_ts":1700003600,"due_label":"3 PM","kind":"event","done":false,"card_id":"c1"},
                  {"id":"t2","title":"Call Mom","source":"messages","account":"","who":"Mom","quote":"tmrw",
                   "due_ts":null,"due_label":"Tomorrow","kind":"call","done":true,"card_id":null}],
         "threads":[{"id":"h1","source":"mail","account":"judge@gmail.com","who":"Team","title":"Judging","summary":"Noon in Hall B",
                     "needs_reply":false,"received_ts":1700000000,"reply_ref":{"message_id":"<g1>","account":"judge@gmail.com","account_id":7}},
                    {"id":"h2","source":"messages","account":"","who":"Mom","title":"","summary":"Talk tmrw",
                     "needs_reply":true,"received_ts":1600000000,"reply_ref":"msg"}]}
        """)
        check(full != nil, "the contract decodes")
        guard let full else { exit(1) }
        check(full.state == "ready" && !full.isWorking && !full.isUnavailable && full.asOf == 1_700_000_000, "state")
        check(full.progress == .init(done: 12, total: 40) && full.progressLabel == "Reading 12 of 40…"
              && full.progressFraction == 0.3, "progress: \(full.progressLabel)")
        check(full.accounts == ["me@ucsc.edu", "judge@gmail.com"] && full.summary == "A steady day.", "accounts and summary")
        let card = full.cards.first
        check(card?.metaLine == "OVERDUE · 3 DAYS · MAIL" && card?.title == "Reply to Dana" && card?.sources.first?.quote == "coffee?",
              "a card's meta line is caps: \(card?.metaLine ?? "")")
        check(card?.action?.method == "email_reply" && card?.action?.label == "Reply & add to calendar?"
              && card?.action?.draft == "Hi Dana" && card?.action?.recipient == "dana@x.com" && card?.action?.sendsDraft == true,
              "a card's action, ignoring the opaque envelope and event")
        check(full.todos.count == 2 && full.todos[0].dueTs == 1_700_003_600 && full.todos[0].cardID == "c1"
              && full.todos[1].dueTs == nil && full.todos[1].done && full.todos[1].cardID == nil, "to-dos")
        check(full.todos[0].dayInput == DayTodoInput(id: "t1", title: "Coffee chat", source: "mail", who: "Dana",
                                                     due: 1_700_003_600, dueLabel: "3 PM", kind: "event", done: false),
              "a to-do as the Day takes it")
        check(full.threads.count == 2 && full.threads[0].canReply && !full.threads[1].canReply
              && full.threads[0].replyRef == ["message_id": "<g1>", "account": "judge@gmail.com", "account_id": "7"]
              && full.threads[0].replyAccount == "judge@gmail.com" && full.threads[0].reference == "<g1>",
              "reply_ref decodes as a string map; only mail threads with a message id reply")

        // Absent, null and malformed never fail the workspace.
        check(workspace("{}").map { $0.proactive == nil } == true, "an older service sends no proactive")
        check(workspace(#"{"proactive":null}"#).map { $0.proactive == nil } == true, "null is none")
        let odd = workspace(#"{"proactive":"soon"}"#)?.proactive
        check(odd != nil && odd!.isUnavailable && odd!.cards.isEmpty, "a non-object reads as unavailable, not an error")
        let partial = decode("""
        {"state":"working","cards":[{"id":"ok","title":"Fine"},{"title":"no id"},7,{"id":3,"title":"numeric id",
         "action":"oops"}],"todos":[{"id":"a"},{"id":"b","title":"Has title","due_ts":"tomorrow","done":"yes"}],
         "threads":{"not":"a list"},"accounts":["x@y.z",5,""],"progress":{"done":"many"}}
        """)
        check(partial?.isWorking == true, "partial: working")
        check(partial?.cards.map(\.id) == ["ok", "3"] && partial?.cards[1].action == nil,
              "bad cards are skipped, a numeric id is kept, a bad action is dropped: \(partial?.cards.map(\.id) ?? [])")
        check(partial?.todos.map(\.id) == ["b"] && partial?.todos[0].dueTs == nil && partial?.todos[0].done == false,
              "a to-do needs a title; wrong-typed fields fall back")
        check(partial?.threads.isEmpty == true && partial?.accounts == ["x@y.z"], "a non-list and stray values are dropped")
        check(partial?.progress == .init(done: 0, total: 0) && partial?.progressLabel == "Reading your mail and texts…"
              && partial?.progressFraction == nil, "unknown progress has no count")

        let bare = decode(#"{"threads":[{"id":"z","source":"mail","reply_ref":"<m1>"}],"cards":[{"id":"k","reply_ref":{"message_id":"<m2>"}}]}"#)
        check(bare?.threads.first?.replyRef == ["message_id": "<m1>"] && bare?.cards.first?.replyRef == ["message_id": "<m2>"],
              "a bare string reference is the message id; cards carry one too")

        // Accounts: untagged items belong to the first (the person's own).
        let mine = full.only("me@ucsc.edu"), judge = full.only("judge@gmail.com"), all = full.only(nil)
        check(mine.cards.count == 1 && mine.todos.count == 2 && mine.threads.map(\.id) == ["h2"], "my account keeps untagged items")
        check(judge.cards.isEmpty && judge.todos.isEmpty && judge.threads.map(\.id) == ["h1"], "the judge's account is only theirs")
        check(all == full, "All shows everything")
        check(full.belongs("JUDGE@gmail.com", to: "judge@gmail.com"), "accounts compare without case")
        check(full.chipName("judge@gmail.com") == "judge"
              && ProactiveState(accounts: ["a@x.com", "a@y.com"]).chipName("a@x.com") == "a@x.com",
              "chip names are short unless two clash")

        // Order: open to-dos soonest first, undated last, done at the bottom; replies first, newest first.
        let order = ProactiveState(todos: [
            .init(id: "1", title: "undated", source: "mail"),
            .init(id: "2", title: "done", source: "mail", dueTs: 5, done: true),
            .init(id: "3", title: "later", source: "mail", dueTs: 20),
            .init(id: "4", title: "soon", source: "mail", dueTs: 10),
        ], threads: [
            .init(id: "a", source: "mail", needsReply: false, receivedTs: 30),
            .init(id: "b", source: "mail", needsReply: true, receivedTs: 10),
            .init(id: "c", source: "mail", needsReply: true, receivedTs: 20),
        ])
        check(order.orderedTodos.map(\.id) == ["4", "3", "1", "2"], "to-do order: \(order.orderedTodos.map(\.id))")
        check(order.orderedThreads.map(\.id) == ["c", "b", "a"], "thread order: \(order.orderedThreads.map(\.id))")

        // Button words never come back empty.
        check(ProactiveCard.Action(method: "reminder").label == "Add the reminder?"
              && ProactiveCard.Action(method: "read", button: "  ").label == "Got it", "fallback button words")
        check(ProactiveCard(id: "x", sources: [.init(kind: "messages")]).metaLine == "TEXTS"
              && ProactiveCard(id: "y").metaLine == "BEFORE YOU ASK", "fallback meta lines")
        check(ProactiveThread(id: "m", source: "mail", title: "Subject", replyRef: ["message_id": "r"]).canReply
              && !ProactiveThread(id: "m", source: "mail", replyRef: ["account": "a"]).canReply
              && ProactiveThread(id: "m", source: "mail", title: "Subject").line == "Subject", "threads")

        // What the service said.
        check(ProactiveOutcome.parse(Data(#"{"ok":true}"#.utf8)).ok, "ok")
        check(ProactiveOutcome.parse(Data("{}".utf8)).ok, "a bare 2xx is ok")
        let failed = ProactiveOutcome.parse(Data(#"{"ok":false,"error":"Mail isn't set up"}"#.utf8))
        check(!failed.ok && failed.message == "Mail isn't set up", "a refused fire keeps the card, with the reason")
        check(!ProactiveOutcome.parse(Data(#"{"error":"send_unavailable"}"#.utf8)).ok, "an error alone is a failure")
        let handed = ProactiveOutcome.parse(Data(#"{"ok":true,"result":"open_in_calendar","detail":"Finish in Calendar"}"#.utf8))
        check(handed.ok && handed.handoff == "open_in_calendar" && handed.message == "Finish in Calendar", "a handoff")
        check(ProactiveOutcome.parse(Data(#"{"ok":true,"open_url":"https://mail.google.com/"}"#.utf8)).url == "https://mail.google.com/",
              "an Open in Gmail handoff")
        check(!ProactiveOutcome.parse(Data("not json".utf8)).ok == false, "unparseable 2xx is ok (no error named)")

        print("Proactive checks: \(count) passed")
    }
}
