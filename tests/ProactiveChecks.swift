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
         "accounts":["me","judge@gmail.com"],"summary":"A steady day.",
         "cards":[{"id":"c1","meta":"Overdue · 3 days · Mail","title":"Reply to Dana","body":"I checked…","why":"Asked twice",
                   "account":"me","sources":[{"kind":"mail","ref":"m1","who":"Dana","quote":"coffee?"}],
                   "action":{"method":"email_reply","button":"Reply & add to calendar?","draft":"Hi Dana","recipient":"dana@x.com",
                             "event":{"start":1},"envelope":{"opaque":true}}}],
         "todos":[{"id":"t1","title":"Coffee chat","source":"mail","account":"me","who":"Dana","quote":"coffee?",
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
        check(full.accounts == ["me", "judge@gmail.com"] && full.summary == "A steady day.", "accounts and summary")
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
        // Catching up after a long absence (return_refresh): only a real true counts.
        check(full.updating == false && partial?.updating == false, "updating defaults to false")
        check(decode(#"{"state":"ready","updating":true}"#)?.updating == true, "updating decodes")
        check(decode(#"{"state":"ready","updating":"yes"}"#)?.updating == false, "a wrong-typed updating is false")
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
        let mine = full.only("me"), judge = full.only("judge@gmail.com"), all = full.only(nil)
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
        // A "Wisp did this" card says who acted, and its button is a plain Undo, not a question.
        let did = try? JSONDecoder().decode(ProactiveCard.self, from: Data("""
            {"id":"card:d","meta":"ADDED · CALENDAR","title":"Added Dinner with Maya to your calendar",
             "action":{"method":"undo_add","button":"Undo?"}}
            """.utf8))
        check(did?.wispDidThis == true && did?.metaLine == "WISP DID THIS · CALENDAR"
              && did?.action?.undoLabel == "Undo", "a done card's meta line: \(did?.metaLine ?? "")")
        check(ProactiveCard.didThisLine("ADDED · REMINDER") == "WISP DID THIS · REMINDER"
              && ProactiveCard.didThisLine("") == "WISP DID THIS"
              && ProactiveCard.didThisLine("REMINDER") == "WISP DID THIS · REMINDER"
              && ProactiveCard.didThisLine("done") == "WISP DID THIS"
              && ProactiveCard(id: "z").wispDidThis == false, "done-card meta line fallbacks")
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

        // A reply that wasn't sent: the card shows the service's sentence, never
        // its code, and what it offers next follows the status.
        let confirm = ProactiveOutcome.parse(Data(#"{"ok":false,"status":"confirm","error":"recipients_changed","result":"Mail will send this to a@b.c. Tap Send to confirm.","recipient":"a@b.c"}"#.utf8))
        check(!confirm.ok && confirm.status == "confirm" && confirm.message == "Mail will send this to a@b.c. Tap Send to confirm.",
              "a recipient change reads as its sentence: \(confirm.message)")
        check(confirm.refusal(method: "email_reply") == .confirm("Mail will send this to a@b.c. Tap Send to confirm."),
              "a recipient change keeps Send, for a fresh tap")
        let unconfirmed = ProactiveOutcome.parse(Data(#"{"ok":false,"status":"unknown","error":"unconfirmed","result":"Mail didn't confirm the reply. Check Mail before trying again."}"#.utf8))
        check(unconfirmed.message == "Mail didn't confirm the reply. Check Mail before trying again."
              && unconfirmed.refusal(method: "email_reply") == .unconfirmed(unconfirmed.message),
              "an unconfirmed send reads as its sentence and offers no second Send: \(unconfirmed.message)")
        let locked = ProactiveOutcome.parse(Data(#"{"ok":false,"status":"locked","error":"Check Messages before sending again: the last send wasn't confirmed."}"#.utf8))
        check(locked.refusal(method: "text_reply") == .unconfirmed("Check Messages before sending again: the last send wasn't confirmed."),
              "a locked card offers no second Send")
        let crashed = ProactiveOutcome.parse(Data(#"{"ok":false,"status":"unknown","error":"KeyError"}"#.utf8))
        check(crashed.message.isEmpty
              && crashed.refusal(method: "email_reply") == .unconfirmed("Wisp couldn't confirm the reply. Check Mail before trying again.")
              && crashed.refusal(method: "text_reply") == .unconfirmed("Wisp couldn't confirm the text. Check Messages before trying again."),
              "a bare code is never shown; an unknown reply says where to check")
        let refused = ProactiveOutcome.parse(Data(#"{"ok":false,"status":"refused","error":"prepare_failed","result":"Mail couldn't prepare this reply. Nothing was sent."}"#.utf8))
        check(refused.refusal(method: "email_reply") == .retry("Mail couldn't prepare this reply. Nothing was sent."),
              "a refused reply can be tried again, with the reason")
        let unsentText = ProactiveOutcome.parse(Data(#"{"ok":false,"status":"failed","error":"The text wasn't sent. Try again.","result":"(the message was NOT sent: unknown error)"}"#.utf8))
        check(unsentText.message == "The text wasn't sent. Try again.", "a sentence in error wins over the send tool's own line")
        check(ProactiveOutcome.parse(Data(#"{"ok":false,"error":"send_unavailable","result":"open_in_mail"}"#.utf8))
                .refusal(method: "email_reply") == .retry("That didn't go through. Try again."),
              "neither a code nor a handoff token is shown")
        check(ProactiveOutcome.parse(Data(#"{"ok":false,"status":"unknown","error":"Native result was not confirmed"}"#.utf8))
                .refusal(method: "reminder") == .retry("Native result was not confirmed"),
              "only a reply is ever held back from a second try")
        check(handed.message == "Finish in Calendar"
              && ProactiveOutcome.parse(Data(#"{"ok":true,"status":"sent","result":"Reply sent to a@b.c."}"#.utf8)).message.isEmpty,
              "a success never takes its words from result")

        // A reply's fire waits as long as the service may take (two round trips
        // to Mail, 45 seconds each); no answer in that time is not "it failed".
        check(ProactiveCard.Action(method: "email_reply").fireTimeout == 120
              && ProactiveCard.Action(method: "text_reply").fireTimeout == 120
              && ProactiveCard.Action(method: "reminder").fireTimeout == 60, "a reply's fire gets the composer's 120 seconds")
        check(!ProactiveOutcome.neverReached(URLError(.timedOut)) && !ProactiveOutcome.neverReached(URLError(.networkConnectionLost))
              && !ProactiveOutcome.neverReached(CocoaError(.fileNoSuchFile)),
              "a timeout or a dropped connection doesn't prove a reply wasn't sent")
        check(ProactiveOutcome.neverReached(URLError(.cannotConnectToHost)) && ProactiveOutcome.neverReached(URLError(.badURL)),
              "a request that never reached the service sent nothing")

        // One sentence on top, the work behind it, and the three rows' lines.
        let top = decode(#"{"lede":"Hack Day fills your midday. More later.","summary":"Ignored.","stats":{"read":49,"found":3,"drafted":1,"as_of":5}}"#)
        check(top?.headline == "Hack Day fills your midday." && top?.stats == .init(read: 49, found: 3, drafted: 1, asOf: 5),
              "the lede wins and is cut to one sentence: \(top?.headline ?? "")")
        check(top?.workLine == "Wisp read 49 emails and texts · found 3 things · drafted 1 reply", "work line: \(top?.workLine ?? "")")
        check(top?.workLine(short: true) == "Wisp read 49 emails and texts · drafted 1 reply",
              "the short work line leaves out what was found: \(top?.workLine(short: true) ?? "")")
        check(ProactiveState(summary: "Dr. Lee moved it to 4:15. Mom called.").headline == "Dr. Lee moved it to 4:15.",
              "titles and clocks don't end a sentence")
        check(ProactiveState(summary: "Call Mom. Then rest.").headline == "Call Mom.", "a short word still ends one")
        check(ProactiveState().headline == nil && ProactiveState().workLine == nil, "no lede, summary or stats: nothing")
        check(top?.footLine == "Wisp read 49 · found 3", "the footer's short read line: \(top?.footLine ?? "")")
        check(decode(#"{"stats":{"read":12,"found":2,"drafted":0,"filtered":5}}"#)?.footLine == "Wisp read 12 · skipped 5 · found 2",
              "skipped newsletters in the footer")
        check(ProactiveState().footLine == nil && decode(#"{"stats":{"read":0}}"#)?.footLine == nil, "nothing read: no footer line")
        check(decode(#"{"stats":{"read":1,"found":1,"drafted":0}}"#)?.workLine == "Wisp read 1 email and texts · found 1 thing",
              "singular, and no drafted part at zero")
        check(decode(#"{"stats":{"read":0}}"#)?.workLine == nil && decode(#"{"stats":"x"}"#)?.stats == nil, "nothing read hides the line")
        let rows = ProactiveState(todos: [
            .init(id: "1", title: "Readings", source: "reminders"), .init(id: "2", title: "readings!", source: "reminders"),
            .init(id: "3", title: "Worksheet 3", source: "reminders"), .init(id: "4", title: "Done", source: "mail", done: true),
        ], threads: [
            .init(id: "a", source: "mail", who: "Priya", receivedTs: 9),
            .init(id: "b", source: "mail", who: "Ed", needsReply: true, receivedTs: 10),
            .init(id: "c", source: "messages", who: "Mom", needsReply: true, receivedTs: 8),
            .init(id: "d", source: "mail", who: "Devarsh", needsReply: true, receivedTs: 1),
            .init(id: "e", source: "mail", who: "Ed", receivedTs: 2), .init(id: "f", source: "mail", who: "Lee", receivedTs: 3),
            .init(id: "g", source: "mail", who: "Team", receivedTs: 4),
        ])
        check(rows.todoLine == "3 to-dos · Readings ×2, Worksheet 3", "to-do row: \(rows.todoLine)")
        check(rows.peopleLine == "Mail and texts · Ed, Mom, Devarsh, +3", "people row: \(rows.peopleLine)")
        check(ProactiveState().todoLine == "No to-dos" && ProactiveState().peopleLine == "Mail and texts · nothing new", "empty rows")
        let block = decode(#"{"cards":[{"id":"b","title":"Worksheet 3","action":{"method":"block_time","slot":{"start":100,"end":200}}},{"id":"x","action":{"method":"block_time","slot":{"start":5,"end":1}}}]}"#)
        check(block?.cards.first?.action?.slot == .init(start: 100, end: 200) && block?.cards.first?.action?.label == "Block the time?"
              && block?.cards.last?.action?.slot == nil, "a block-time card holds its slot; a backwards slot is dropped")
        check(block?.cards.first?.chipTitle == "Worksheet 3", "chips name the card")

        let ranked = ProactiveState(threads: [
            .init(id: "org", source: "mail", who: "Hackathon Team", receivedTs: 50),
            .init(id: "fyi", source: "mail", who: "Priya", receivedTs: 10),
            .init(id: "reply-org", source: "mail", who: "UCSC Admissions", needsReply: true, receivedTs: 40),
            .init(id: "reply", source: "messages", who: "Mom", needsReply: true, receivedTs: 5),
        ])
        check(ranked.orderedThreads.map(\.id) == ["reply", "reply-org", "fyi", "org"],
              "replies first, then people over organizations, then newest: \(ranked.orderedThreads.map(\.id))")
        check(ProactiveThread(id: "x", source: "mail", who: "no-reply@canvas").isOrganization
              && !ProactiveThread(id: "y", source: "mail", who: "Ed").isOrganization, "organizations by name")

        // Each to-do and thread's trailing ×: Clear for every row the service
        // fingerprinted, Mark done only for an open dated reminder.
        let actionable = decode("""
        {"state":"ready","todos":[
          {"id":"todo:r","title":"Wisp verification timed 2113","source":"reminders","due_ts":1700000000,
           "due_label":"Overdue · 2 days","kind":"reminder","done":false,"fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaa","can_complete":true},
          {"id":"todo:m","title":"Reply to Dana","source":"mail","fingerprint":"bbbbbbbbbbbbbbbbbbbbbbbb"},
          {"id":"todo:d","title":"Done one","source":"reminders","done":true,"fingerprint":"cccccccccccccccccccccccc",
           "can_complete":true},
          {"id":"todo:old","title":"Older service","source":"reminders","can_complete":true}],
         "threads":[{"id":"thread:t","source":"messages","who":"Mom","title":"Dinner Sunday?",
                     "fingerprint":"dddddddddddddddddddddddd"},
                    {"id":"thread:o","source":"mail","who":"Ed","title":"Old"}]}
        """)
        check(actionable?.todos.map(\.actionRow.actions) == [[.clear, .markDone], [.clear], [.clear], []],
              "to-do actions: \(actionable?.todos.map(\.actionRow.actions) ?? [])")
        check(actionable?.todos.first?.actionRow.route == NeedsYouRow.todoRoute
              && actionable?.todos.first?.actionRow.fingerprint == "aaaaaaaaaaaaaaaaaaaaaaaa", "a to-do's route and fingerprint")
        check(actionable?.threads.map(\.actionRow.actions) == [[.clear], []], "a thread offers Clear only")
        check(actionable?.threads.first?.actionRow.route == NeedsYouRow.threadRoute
              && actionable?.threads.first?.actionRow.text == "Mom: Dinner Sunday?", "a thread's route and label text")
        check(actionable?.threads.first?.actionRow.help(.clear).contains("Nothing is marked read") == true
              && actionable?.todos.first?.actionRow.help(.clear).contains("stays in Reminders") == true,
              "each Clear says what it leaves alone")

        // A reply never sends a draft the person hasn't seen: the first tap opens
        // it, and Send (inside the shown draft) sends it.
        let reply = ProactiveCard.Action(method: "text_reply", button: "Text Maya back?", draft: "Sounds good!")
        check(reply.primaryStep(draftOpen: false) == .openDraft && reply.primaryLabel(draftOpen: false) == "Text Maya back?",
              "a closed reply's button opens the draft")
        check(reply.primaryStep(draftOpen: true) == .fire && reply.primaryLabel(draftOpen: true) == "Send",
              "an open reply's button is Send")
        let reminder = ProactiveCard.Action(method: "reminder", button: "Set the reminder?")
        check(reminder.primaryStep(draftOpen: false) == .fire && reminder.primaryLabel(draftOpen: false) == "Set the reminder?",
              "a card with nothing to send fires on its first tap")

        // Unread sources: no all-clear for what Wisp couldn't read.
        let coverage = [SourceAccess(source: "calendar", ready: false, blocked: "calendar_access"),
                        SourceAccess(source: "reminders", ready: true),
                        SourceAccess(source: "email", ready: false, state: "unavailable", blocked: "mail_closed"),
                        SourceAccess(source: "messages", ready: false, blocked: "full_disk_access")]
        let ownOnly = ProactiveState(state: "ready")
        check(ownOnly.unreadSources(coverage).map { $0.source } == ["calendar", "email", "messages"], "unread in reading order")
        // A linked account alone isn't a read: it may be expired or failing.
        var linked = ProactiveState(state: "ready", accounts: ["me", "ada@gmail.com"])
        linked.ownerID = "me"
        check(linked.unreadSources(coverage).map { $0.source } == ["calendar", "email", "messages"],
              "a linked Google account that hasn't read recently doesn't count")
        linked.mailReadBy = ["ada@gmail.com"]
        check(linked.unreadSources(coverage).map { $0.source } == ["calendar", "messages"],
              "mail a connected Google account read recently counts as read")
        let decoded = decode(#"{"state":"ready","accounts":["me","ada@gmail.com"],"mail_read_by":["ada@gmail.com"],"mail_unread_by":["work@gmail.com"]}"#)
        check(decoded?.mailReadBy == ["ada@gmail.com"] && decoded?.mailUnreadBy == ["work@gmail.com"], "mail_read_by and mail_unread_by decode")
        // One account read, one unchecked: mail is only partly read, even when Mail.app is ready.
        linked.mailUnreadBy = ["work@gmail.com"]
        let partly = linked.unreadSources(coverage.map { $0.source == "email" ? SourceAccess(source: "email", ready: true) : $0 })
        check(partly.map { $0.source } == ["calendar", "email", "messages"]
              && partly.first { $0.source == "email" }?.headline == "Wisp hasn't finished checking work@gmail.com yet",
              "an unchecked Google account keeps mail unread, by name")
        linked.mailUnreadReasons = ["work@gmail.com": "reconnect"]
        check(linked.unreadSources(coverage).first { $0.source == "email" }?.headline == "Couldn't check work@gmail.com",
              "reconnect only when the service says so")
        check(decode(#"{"state":"ready","mail_unread_by":["w@x.com"],"mail_unread_reasons":{"w@x.com":"reconnect"}}"#)?
                .uncheckedMail == UncheckedMail(reconnect: ["w@x.com"]), "mail_unread_reasons decodes")
        check(ProactiveState.attentionEmptyLine(unread: partly) == nil, "no inbox all-clear while an account is unchecked")
        check(ProactiveState.attentionEmptyLine(unread: ownOnly.unreadSources(coverage)) == nil,
              "no \"Nothing in your inbox or texts\" while either is unread")
        check(ProactiveState.attentionEmptyLine(unread: []) == "Nothing in your inbox or texts needs you.", "the all-clear when both were read")
        check(ownOnly.todoLine(unread: ownOnly.unreadSources(coverage)) == "No to-dos in what Wisp could read"
              && ownOnly.todoLine(unread: []) == "No to-dos", "the to-do row is honest about what was read")
        check(ProactiveState.emptyDayLine(calendarUnread: true) == "Wisp can't see your calendar yet"
              && ProactiveState.emptyDayLine(calendarUnread: false) == "Nothing on your calendar today"
              && ProactiveState.emptyDayLine(calendarUnread: false, earlierToday: 1) == "Nothing left on your calendar today",
              "the calendar row: never \"else\" with nothing listed above it")
        // B1: who a row is from, as shown; the service's text is kept for Clear.
        let groupTodo = ProactiveTodo(id: "g", title: "Pick a time", source: "messages", who: "Lee in of 3 (Dana, Lee, Sam)")
        check(groupTodo.shownWho == "Lee in a group of 3 (Dana, Lee, Sam)" && groupTodo.who == "Lee in of 3 (Dana, Lee, Sam)"
              && groupTodo.dayInput.who == groupTodo.shownWho, "a group text's to-do names the group, in the Brief and on Day")
        let groupThread = ProactiveThread(id: "t", source: "messages", who: "Lee in ")
        check(groupThread.shownWho == "Lee" && groupThread.actionRow.text.hasPrefix("Lee in"), "a thread's sender without a bare \"in\"")
        let nothingRead = decode(#"{"state":"ready","stats":{"read":0},"accounts":[],"reason":"Wisp hasn't read your mail and texts yet. Tap Analyze now.","unread":["calendar","email"]}"#)
        check(nothingRead?.hasReadNothing == true && nothingRead?.reason.hasPrefix("Wisp hasn't read") == true
              && nothingRead?.unread == ["calendar", "email"], "ready with nothing read is not an all-clear; reason and unread decode")
        check(decode(#"{"state":"ready","stats":{"read":4}}"#)?.hasReadNothing == false, "a cycle that read something")

        // R6: a reply that has a card is not also a mail row.
        let replyCard = ProactiveCard(id: "c-ed", title: "Reply to Ed", sources: [ProactiveCard.Source(kind: "mail", ref: "ed-1")],
                                      action: ProactiveCard.Action(method: "email_reply", button: "Send", draft: "Hi"))
        let edThread = ProactiveThread(id: "thread:ed-1", source: "mail", who: "Ed", needsReply: true)
        let momThread = ProactiveThread(id: "thread:mom-1", source: "messages", who: "Mom", needsReply: true)
        check(ProactiveState.threads([edThread, momThread], coveredBy: [replyCard]).map(\.id) == ["thread:mom-1"],
              "a reply card's thread (by its source ref) leaves the mail rows; the others stay")
        var byMessage = ProactiveCard(id: "c-ed2", title: "Reply to Ed",
                                      action: ProactiveCard.Action(method: "email_reply", button: "Send", draft: "Hi"))
        byMessage.replyRef = ["message_id": "<ed@x>"]
        let edMail = ProactiveThread(id: "t-ed", source: "mail", who: "Ed", needsReply: true, replyRef: ["message_id": "<ed@x>"])
        check(ProactiveState.threads([edMail, momThread], coveredBy: [byMessage]).map(\.id) == ["thread:mom-1"],
              "a reply card's thread (by its message id) leaves the mail rows")
        let reminderCard = ProactiveCard(id: "c-exam", title: "Remind me", sources: [ProactiveCard.Source(kind: "mail", ref: "ed-1")],
                                         action: ProactiveCard.Action(method: "reminder", button: "Set"))
        check(ProactiveState.threads([edThread], coveredBy: [reminderCard]).map(\.id) == ["thread:ed-1"],
              "a card that isn't a reply keeps its thread")
        check(ProactiveState.threads([edMail], coveredBy: []).map(\.id) == ["t-ed"], "no cards, every thread")

        // R6: school work and to-dos as one list, most urgent first.
        let day0: Double = 1_700_000_000, noon = day0 + 12 * 3600
        let plate = ProactiveState.plate(
            todos: [ProactiveTodo(id: "later", title: "Later", source: "reminders", dueTs: day0 + 3 * 86400, dueLabel: "Tue"),
                    ProactiveTodo(id: "late", title: "Late", source: "reminders", dueTs: day0 - 86400, dueLabel: "Overdue · 1 day"),
                    ProactiveTodo(id: "none", title: "Undated", source: "mail"),
                    ProactiveTodo(id: "tom", title: "Tomorrow", source: "reminders", dueTs: day0 + 86400 + 3600, dueLabel: "Tomorrow")],
            school: [StudyDaySummary.Due(id: "lab", title: "Lab 5", dueTS: day0 + 23 * 3600),
                     StudyDaySummary.Due(id: "quiz", title: "Quiz", dueTS: day0 + 86400 + 600)],
            isDone: { _ in false }, now: noon, dayStart: day0, todayEnd: day0 + 86400, tomorrowEnd: day0 + 2 * 86400)
        check(plate.map(\.id) == ["todo:late", "school:lab", "school:quiz", "todo:tom", "todo:later", "todo:none"],
              "overdue, today, tomorrow (by time), later, undated: \(plate.map(\.id))")
        check(plate.filter(\.isSchool).count == 2, "school rows keep their kind")
        // R6 polish: an event the calendar line already names is not also a to-do row.
        let events = [ProactiveTodo(id: "hack", title: "Hack Day", source: "calendar", dueTs: noon, dueLabel: "12 PM", kind: "event"),
                      ProactiveTodo(id: "fair", title: "Career fair", source: "calendar", dueTs: day0 + 86400 + 3600, kind: "event"),
                      ProactiveTodo(id: "prep", title: "Hack Day", source: "reminders", dueTs: noon, kind: "task")]
        let named = ProactiveState.plate(todos: events, school: [], isDone: { _ in false }, now: day0, dayStart: day0,
                                         todayEnd: day0 + 86400, tomorrowEnd: day0 + 2 * 86400, named: ["hack day!"])
        check(named.map(\.id) == ["todo:prep", "todo:fair"],
              "the named event leaves the list; tomorrow's event and a to-do sharing its name stay: \(named.map(\.id))")
        check(ProactiveState.plate(todos: events, school: [], isDone: { _ in false }, now: day0, dayStart: day0,
                                   todayEnd: day0 + 86400, tomorrowEnd: day0 + 2 * 86400).count == 3,
              "with nothing named, every event stays")

        print("Proactive checks: \(count) passed")
    }
}
