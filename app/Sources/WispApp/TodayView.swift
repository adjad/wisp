import SwiftUI
import AppKit

struct TodayTask: Decodable, Identifiable {
    let id: String
    let title: String
    let day: String
    let timezone: String?
    let kind: String
    let duration_minutes: Int
    let priority: Int
    let due_ts: Double?
    let pinned_start: Double?
    let status: String
    let revision: Int
    var editingTimeZone: TimeZone { TimeZone(identifier: timezone ?? "UTC") ?? TimeZone(secondsFromGMT: 0)! }

    func editFields(title: String, kind: String, priority: Int, minutes: Int, due: Date?, pin: Date?) -> [String: Any] {
        // Metadata edits preserve the task's original date/timezone contract.
        ["title": title, "kind": kind, "priority": priority, "duration_minutes": minutes,
         "due_ts": due.map { $0.timeIntervalSince1970 as Any } ?? NSNull(),
         "pinned_start": pin.map { $0.timeIntervalSince1970 as Any } ?? NSNull()]
    }
}
struct TodayBlock: Decodable, Identifiable {
    let id: String
    let title: String
    let start: Double
    let end: Double?
    let kind: String
    let task_id: String?
    let warnings: [String]
}
struct TodayDeadline: Decodable, Identifiable {
    let id: String
    let title: String
    let due_ts: Double
    let source: String
}
struct TodayUnscheduled: Decodable, Identifiable {
    let task_id: String
    let title: String
    let reason: String
    var id: String { task_id }
}
struct TodayPreferences: Decodable {
    let start_minute: Int
    let end_minute: Int
    let not_before: Double?
}
struct TodaySource: Decodable {
    let ready: Bool
    let last_sync: Double?
    let reason: String
}
struct TodayPlan: Decodable {
    let day: String
    let timezone: String
    let generated_at: Double
    let revision: Int
    let provisional: Bool
    let warnings: [String]
    let preferences: TodayPreferences
    let sources: [String: TodaySource]
    let tasks: [TodayTask]
    let blocks: [TodayBlock]
    let deadlines: [TodayDeadline]
    let all_day: [TodayBlock]
    let unscheduled: [TodayUnscheduled]
}

@MainActor
final class TodayModel: ObservableObject {
    @Published var plan: TodayPlan?
    @Published var error = ""
    @Published var busy = false
    @Published private(set) var selectedDate: Date
    private var generation = 0
    private var manualDay: String?
    private var displayedKey: DayKey
    private let nowProvider: () -> Date
    private let zoneProvider: () -> TimeZone
    var transport: (URLRequest) async throws -> (Data, URLResponse)

    private struct DayKey: Equatable {
        let day: String
        let zone: String
    }

    init(timezone: TimeZone? = nil, now: @escaping () -> Date = Date.init,
         timeZoneProvider: (() -> TimeZone)? = nil,
         transport: @escaping (URLRequest) async throws -> (Data, URLResponse) = { try await URLSession.shared.data(for: $0) }) {
        nowProvider = now
        zoneProvider = timeZoneProvider ?? { timezone ?? .autoupdatingCurrent }
        let current = now()
        let zone = timeZoneProvider?() ?? timezone ?? .autoupdatingCurrent
        selectedDate = current
        displayedKey = DayKey(day: Self.dayString(current, in: zone), zone: zone.identifier)
        self.transport = transport
    }

    var timezone: TimeZone { zoneProvider() }
    var followsToday: Bool { manualDay == nil }
    var day: String { currentKey.day }
    var pickerDay: String { Self.dayString(selectedDate, in: timezone) }
    var isShowingCurrentDay: Bool { day == Self.dayString(nowProvider(), in: timezone) }

    private static func dayString(_ date: Date, in zone: TimeZone) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = zone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }

    private var currentKey: DayKey {
        let zone = timezone
        return DayKey(day: manualDay ?? Self.dayString(nowProvider(), in: zone), zone: zone.identifier)
    }

    private static func pickerDate(for day: String, in zone: TimeZone) -> Date {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return Date() }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12)) ?? Date()
    }

    // A manual civil day remains selected even when the system's time zone changes.
    @discardableResult
    func reconcileClock() -> Bool {
        let key = currentKey
        guard key != displayedKey else { return false }
        displayedKey = key
        generation += 1
        plan = nil
        error = ""
        selectedDate = manualDay == nil ? nowProvider() : Self.pickerDate(for: key.day, in: timezone)
        return true
    }

    func selectDate(_ date: Date) {
        reconcileClock()
        let zone = timezone
        let chosenDay = Self.dayString(date, in: zone)
        let today = Self.dayString(nowProvider(), in: zone)
        manualDay = chosenDay == today ? nil : chosenDay
        selectedDate = date
        reconcileClock()
    }

    func returnToToday() {
        manualDay = nil
        reconcileClock()
        selectedDate = nowProvider()
    }

    func clock(_ timestamp: Double) -> String {
        let formatter = DateFormatter()
        formatter.timeZone = timezone
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter.string(from: Date(timeIntervalSince1970: timestamp))
    }

    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: WispClient.baseURL) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 15
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await transport(req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw NSError(domain: "Today", code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                          userInfo: [NSLocalizedDescriptionKey: detail?["detail"] as? String ?? "Could not update your day. Try again."])
        }
        return data
    }

    func refresh(afterMutation: Bool = false) async {
        reconcileClock()
        guard !busy || afterMutation else { return }
        generation += 1
        let ticket = generation
        let selected = currentKey
        var components = URLComponents()
        components.path = "/assistant/today"
        components.queryItems = [URLQueryItem(name: "day", value: selected.day), URLQueryItem(name: "timezone", value: selected.zone)]
        do {
            let next = try JSONDecoder().decode(TodayPlan.self, from: await request(components.string!))
            guard ticket == generation, selected == currentKey else { return }
            plan = next
            error = ""
        } catch {
            guard ticket == generation, selected == currentKey else { return }
            self.error = error.localizedDescription
            // A failed refresh must not leave stale slots looking actionable.
            plan = nil
        }
    }

    @discardableResult
    func mutate(_ path: String, method: String, body: [String: Any]) async -> Bool {
        guard !busy else { return false }
        busy = true
        generation += 1 // invalidate any in-flight refresh before writing
        var failure: String?
        do { _ = try await request(path, method: method, body: body) }
        catch { failure = error.localizedDescription }
        await refresh(afterMutation: true)
        busy = false
        if let failure { error = failure }
        return failure == nil
    }

    func add(title: String, kind: String, minutes: Int, priority: Int, due: Date?) async -> Bool {
        var body: [String: Any] = ["title": title, "day": day, "timezone": timezone.identifier, "kind": kind,
                                   "duration_minutes": minutes, "priority": priority]
        if let due { body["due_ts"] = due.timeIntervalSince1970 }
        return await mutate("/assistant/today/tasks", method: "POST", body: body)
    }

    @discardableResult
    func edit(_ task: TodayTask, values: [String: Any]) async -> Bool {
        var body = values
        body["revision"] = task.revision
        return await mutate("/assistant/today/tasks/\(task.id)", method: "PATCH", body: body)
    }

    func replan(start: Int, end: Int, delayMinutes: Int? = nil) async {
        guard let plan, plan.day == day else { return }
        var body: [String: Any] = ["day": day, "timezone": timezone.identifier,
                                  "revision": plan.revision, "start_minute": start, "end_minute": end]
        if let delayMinutes {
            body["not_before"] = Date().addingTimeInterval(Double(delayMinutes * 60)).timeIntervalSince1970
        } else {
            body["not_before"] = NSNull() // explicit replan clears a prior delay
        }
        _ = await mutate("/assistant/today/replan", method: "POST", body: body)
    }
}

struct TodayPresentation: Equatable {
    let day: String
    let pickerDay: String
    let timezone: String
    let followsToday: Bool
    let loading: Bool
    let blockTitles: [String]
}

struct TodayView: View {
    @StateObject private var model: TodayModel
    private let onPresentation: ((TodayPresentation) -> Void)?
    @State private var title = ""
    @State private var kind = "study"
    @State private var minutes = 45
    @State private var priority = 2
    @State private var hasDue = false
    @State private var due = Date()
    @State private var startHour = 9
    @State private var endHour = 18
    @State private var hoursDirty = false
    @State private var editingTask: TodayTask?

    @MainActor
    init() { self.init(model: TodayModel()) }

    @MainActor
    init(model: TodayModel, onPresentation: ((TodayPresentation) -> Void)? = nil) {
        _model = StateObject(wrappedValue: model)
        self.onPresentation = onPresentation
    }

    private var visiblePlan: TodayPlan? {
        guard let plan = model.plan, plan.day == model.day,
              plan.timezone == model.timezone.identifier else { return nil }
        return plan
    }

    private var presentation: TodayPresentation {
        let plan = visiblePlan
        return TodayPresentation(day: model.day, pickerDay: model.pickerDay,
                                 timezone: model.timezone.identifier,
                                 followsToday: model.followsToday, loading: plan == nil,
                                 blockTitles: plan?.blocks.map(\.title) ?? [])
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading) {
                    Text("Your day").font(.largeTitle.bold())
                    Text("Make room for what matters.").foregroundStyle(.secondary)
                }
                Spacer()
                DatePicker("Day", selection: Binding(get: { model.selectedDate }, set: { date in
                    model.selectDate(date)
                    hoursDirty = false
                    Task { await model.refresh() }
                }), displayedComponents: .date).labelsHidden()
                    .disabled(model.busy)
                if !model.followsToday {
                    Button("Today") {
                        model.returnToToday()
                        hoursDirty = false
                        Task { await model.refresh() }
                    }.disabled(model.busy)
                }
                Button("Refresh") { Task { await model.refresh() } }.disabled(model.busy)
            }
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red).textSelection(.enabled) }
            if let plan = visiblePlan {
                sourceStatus(plan)
                workingHours(plan)
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        if !plan.warnings.isEmpty {
                            GroupBox("Plan needs attention") {
                                VStack(alignment: .leading, spacing: 4) {
                                    ForEach(plan.warnings, id: \.self) { Text($0).frame(maxWidth: .infinity, alignment: .leading) }
                                }.padding(4)
                            }
                        }
                        taskForm
                        agenda(plan)
                        if !plan.unscheduled.isEmpty {
                            GroupBox("Still to fit") {
                                VStack(alignment: .leading, spacing: 10) {
                                    ForEach(plan.unscheduled) { item in
                                        VStack(alignment: .leading) {
                                            Text(item.title).bold()
                                            Text(item.reason).font(.caption).foregroundStyle(.secondary)
                                        }.frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }.padding(6)
                            }
                        }
                        if !plan.deadlines.isEmpty {
                            GroupBox("Deadlines · these do not reserve time") {
                                VStack(alignment: .leading, spacing: 8) {
                                    ForEach(plan.deadlines) { item in
                                        Text("\(model.clock(item.due_ts))  ·  \(item.title)  (\(item.source))")
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }.padding(6)
                            }
                        }
                        taskList(plan)
                        Text("Calendar and Reminders stay unchanged. Task durations are your estimates. Flexible slots update as your day changes; pin one to keep its time.")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(.trailing, 6)
                }
            } else {
                ContentUnavailableView("Your day is loading", systemImage: "calendar",
                                       description: Text("Wisp’s local service needs to be running."))
            }
        }
        .padding(22).frame(minWidth: 720, minHeight: 620)
        .onAppear { onPresentation?(presentation) }
        .onChange(of: presentation) { _, next in onPresentation?(next) }
        .sheet(item: $editingTask) { task in TodayTaskEditor(task: task, model: model) }
        .task {
            while !Task.isCancelled {
                if model.reconcileClock() { hoursDirty = false }
                await model.refresh()
                do { try await Task.sleep(for: .seconds(30)) } catch { break }
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .NSCalendarDayChanged)) { _ in refreshForClockChange() }
        .onReceive(NotificationCenter.default.publisher(for: .NSSystemTimeZoneDidChange)) { _ in refreshForClockChange() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in refreshForClockChange() }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)) { _ in refreshForClockChange() }
        .onReceive(NotificationCenter.default.publisher(for: TodayWindow.didShowNotification)) { _ in refreshForClockChange() }
        .onChange(of: model.plan?.generated_at) { _, _ in
            if !hoursDirty, let p = model.plan {
                startHour = p.preferences.start_minute / 60
                endHour = p.preferences.end_minute / 60
            }
        }
    }

    private func refreshForClockChange() {
        if model.reconcileClock() { hoursDirty = false }
        Task { await model.refresh() }
    }

    private func sourceStatus(_ plan: TodayPlan) -> some View {
        HStack(spacing: 18) {
            ForEach(["calendar", "reminders"], id: \.self) { source in
                if let info = plan.sources[source] {
                    Label(source.capitalized + ": " + info.reason,
                          systemImage: info.ready ? "checkmark.circle" : "clock.badge.exclamationmark")
                        .font(.caption).foregroundStyle(info.ready ? Color.secondary : Color.orange)
                        .help(info.last_sync.map { "Last sync: \(model.clock($0))" } ?? "No sync yet")
                }
            }
        }
    }

    private func workingHours(_ plan: TodayPlan) -> some View {
        HStack {
            Text("Working hours")
            Picker("Start", selection: $startHour) {
                ForEach(0..<24) { Text(String(format: "%02d:00", $0)).tag($0) }
            }.labelsHidden().frame(width: 88)
            Text("to")
            Picker("End", selection: $endHour) {
                ForEach(1..<25) { Text(String(format: "%02d:00", $0)).tag($0) }
            }.labelsHidden().frame(width: 88)
            Button("Replan") {
                Task { await model.replan(start: startHour * 60, end: endHour * 60); hoursDirty = false }
            }.disabled(startHour >= endHour || model.busy)
            Spacer()
            Button("Running 15 min late") {
                Task { await model.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute, delayMinutes: 15) }
            }.disabled(model.busy || !model.isShowingCurrentDay)
        }
        .onChange(of: startHour) { _, _ in hoursDirty = true }
        .onChange(of: endHour) { _, _ in hoursDirty = true }
    }

    private var taskForm: some View {
        GroupBox("Make time for something") {
            VStack(alignment: .leading, spacing: 10) {
                TextField("Study for an exam, finish a project…", text: $title)
                HStack {
                    Picker("Type", selection: $kind) {
                        Text("Study").tag("study"); Text("Project").tag("project"); Text("Task").tag("task")
                    }.frame(width: 150)
                    Stepper("\(minutes) min estimate", value: $minutes, in: 5...480, step: 5)
                    Picker("Priority", selection: $priority) {
                        Text("High").tag(1); Text("Normal").tag(2); Text("Low").tag(3)
                    }.frame(width: 160)
                }
                HStack {
                    Toggle("Deadline", isOn: $hasDue)
                    if hasDue { DatePicker("Due", selection: $due).labelsHidden() }
                    Spacer()
                    Button("Add to day") {
                        Task {
                            if await model.add(title: title, kind: kind, minutes: minutes, priority: priority, due: hasDue ? due : nil) {
                                title = ""
                            }
                        }
                    }.disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                }
            }.padding(6)
        }.disabled(model.busy)
    }

    private func agenda(_ plan: TodayPlan) -> some View {
        GroupBox("Itinerary") {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(plan.all_day) { block in Text("All day  ·  \(block.title)").foregroundStyle(.secondary) }
                if plan.blocks.isEmpty { Text("No time blocks yet.").foregroundStyle(.secondary) }
                ForEach(plan.blocks) { block in
                    HStack(alignment: .top) {
                        Text(model.clock(block.start) + (block.end.map { "–" + model.clock($0) } ?? "–?"))
                            .font(.caption.monospacedDigit()).frame(width: 148, alignment: .leading)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(block.title).bold()
                            Text(block.kind == "calendar" ? "Calendar" : block.kind == "manual" ? "Fixed Wisp event" : "\(block.kind.capitalized) · estimated duration")
                                .font(.caption).foregroundStyle(.secondary)
                            ForEach(Array(block.warnings.enumerated()), id: \.offset) { _, warning in
                                Text(warning).font(.caption).foregroundStyle(.orange)
                            }
                        }
                        Spacer()
                        if let id = block.task_id, let task = plan.tasks.first(where: { $0.id == id }) {
                            Button(task.pinned_start == nil ? "Pin" : "Unpin") {
                                Task {
                                    let values: [String: Any] = task.pinned_start == nil
                                        ? ["pinned_start": block.start, "day": model.day, "timezone": model.timezone.identifier]
                                        : ["pinned_start": NSNull()]
                                    await model.edit(task, values: values)
                                }
                            }.disabled(model.busy)
                        }
                    }
                    Divider()
                }
            }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func taskList(_ plan: TodayPlan) -> some View {
        GroupBox("Your tasks") {
            VStack(alignment: .leading, spacing: 10) {
                if plan.tasks.isEmpty { Text("Add your first study or project block above.").foregroundStyle(.secondary) }
                ForEach(plan.tasks.filter { $0.status != "dismissed" }) { task in
                    HStack {
                        Button {
                            Task { await model.edit(task, values: ["status": task.status == "done" ? "active" : "done"]) }
                        } label: { Image(systemName: task.status == "done" ? "checkmark.circle.fill" : "circle") }
                        .buttonStyle(.plain).help(task.status == "done" ? "Reopen task" : "Mark done")
                        Text(task.title).strikethrough(task.status == "done")
                        Spacer()
                        Text("\(task.duration_minutes) min")
                        Button("Edit") { editingTask = task }
                        Button("−15") { Task { await model.edit(task, values: ["duration_minutes": max(1, task.duration_minutes - 15)]) } }
                        Button("+15") { Task { await model.edit(task, values: ["duration_minutes": min(1440, task.duration_minutes + 15)]) } }
                        Button("Remove") { Task { await model.edit(task, values: ["status": "dismissed"]) } }
                    }.disabled(model.busy)
                }
            }.padding(6)
        }
    }
}

private struct TodayTaskEditor: View {
    let task: TodayTask
    @ObservedObject var model: TodayModel
    @Environment(\.dismiss) private var dismiss
    @State private var title: String
    @State private var minutes: Int
    @State private var priority: Int
    @State private var kind: String
    @State private var hasDue: Bool
    @State private var due: Date
    @State private var pinned: Bool
    @State private var start: Date

    init(task: TodayTask, model: TodayModel) {
        self.task = task
        self.model = model
        _title = State(initialValue: task.title)
        _minutes = State(initialValue: task.duration_minutes)
        _priority = State(initialValue: task.priority)
        _kind = State(initialValue: task.kind)
        _hasDue = State(initialValue: task.due_ts != nil)
        _due = State(initialValue: Date(timeIntervalSince1970: task.due_ts ?? model.selectedDate.timeIntervalSince1970))
        _pinned = State(initialValue: task.pinned_start != nil)
        _start = State(initialValue: Date(timeIntervalSince1970: task.pinned_start ?? model.selectedDate.timeIntervalSince1970))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Edit task").font(.title2.bold())
            Text("Times shown in \(task.editingTimeZone.identifier)").font(.caption).foregroundStyle(.secondary)
            TextField("Title", text: $title)
            Picker("Type", selection: $kind) {
                Text("Study").tag("study"); Text("Project").tag("project"); Text("Task").tag("task")
            }
            Stepper("Estimated duration: \(minutes) min", value: $minutes, in: 1...1440)
            Picker("Priority", selection: $priority) {
                Text("High").tag(1); Text("Normal").tag(2); Text("Low").tag(3)
            }
            Toggle("Has a deadline", isOn: $hasDue)
            if hasDue { DatePicker("Due", selection: $due) }
            Toggle("Pin a time", isOn: $pinned)
            if pinned {
                DatePicker("Start on \(task.day)", selection: $start)
                Text("Pins keep their time during replanning. Conflicts are shown in the itinerary.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Save") {
                    Task {
                        if await model.edit(task, values: task.editFields(title: title, kind: kind, priority: priority,
                            minutes: minutes, due: hasDue ? due : nil, pin: pinned ? start : nil)) { dismiss() }
                    }
                }.keyboardShortcut(.defaultAction)
                    .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 470).disabled(model.busy)
            .environment(\.timeZone, task.editingTimeZone)
    }
}

@MainActor
enum TodayWindow {
    static let didShowNotification = Notification.Name("WispTodayWindowDidShow")
    private static var window: NSWindow?
    @discardableResult
    static func show(model: TodayModel? = nil, onPresentation: ((TodayPresentation) -> Void)? = nil,
                     present: Bool = true) -> NSWindow {
        if window == nil {
            let created = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 860, height: 800),
                                   styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            created.title = "Wisp · Today"
            created.contentView = NSHostingView(rootView: TodayView(model: model ?? TodayModel(),
                                                                    onPresentation: onPresentation))
            created.isReleasedWhenClosed = false
            created.center()
            window = created
        }
        let existing = window!
        if present {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        NotificationCenter.default.post(name: didShowNotification, object: nil)
        return existing
    }
}
