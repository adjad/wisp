import SwiftUI

extension WispClient {
    /// The hours as the service holds them, or nil when it didn't answer.
    func userHours() async -> UserHours? {
        guard let object = await hoursRequest("GET", path: "assistant/hours").object else { return nil }
        return UserHours(json: object)
    }

    /// The saved hours, or the message to show.
    func saveUserHours(_ hours: UserHours) async -> (hours: UserHours?, error: String?) {
        let reply = await hoursRequest("PATCH", path: "assistant/hours",
                                       body: hours.patch(timezone: TimeZone.current.identifier))
        guard let object = reply.object else {
            return (nil, reply.error ?? "Couldn't save your hours. They're still here; try again.")
        }
        return (UserHours(json: object), nil)
    }

    /// The offer to show now. nil means the service didn't answer, not "no offer".
    func hoursOffer() async -> (offer: HoursMoveOffer?, answered: Bool) {
        let reply = await hoursRequest("POST", path: "assistant/hours/offer",
                                       body: ["timezone": TimeZone.current.identifier])
        guard let object = reply.object else { return (nil, false) }
        return ((object["offer"] as? [String: Any]).flatMap(HoursMoveOffer.init(json:)), true)
    }

    func finishHoursOffer(_ id: String, accepted: Bool) async {
        _ = await hoursRequest("POST", path: "assistant/hours/offer/\(id)/\(accepted ? "accept" : "dismiss")", body: [:])
    }

    private func hoursRequest(_ method: String, path: String,
                              body: [String: Any]? = nil) async -> (object: [String: Any]?, error: String?) {
        var request = URLRequest(url: Self.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 20
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return (nil, "Wisp's service didn't answer. Your answer is still here; try again.")
        }
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (200..<300).contains(http.statusCode) else {
            return (nil, object?["detail"] as? String)
        }
        return (object, nil)
    }
}

/// Working hours, their days and quiet hours, drawn the same in Getting to know you
/// (on the notch's black) and About you (in the window), which passes its own ink.
struct UserHoursEditor: View {
    struct Ink {
        var label = Theme.textPrimary
        var muted = Theme.textMuted
        var fill = Theme.chipFill
        var stroke = Theme.chipStroke
        var onSelected = Color.black
        var selected = Color.white
    }

    @Binding var hours: UserHours
    var ink = Ink()
    /// About you adds the off switches; Getting to know you stays quick.
    var showsOptions = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 8) {
                row("Working hours", window: $hours.working, canEndAtMidnight: true)
                days
                Text("When to plan your day and suggest times.")
                    .font(.system(size: 11)).foregroundStyle(ink.muted)
            }
            VStack(alignment: .leading, spacing: 8) {
                if hours.quietIsOff {
                    Text("No quiet hours").font(.system(size: 12.5, weight: .medium)).foregroundStyle(ink.label)
                } else {
                    row("Quiet hours", window: $hours.quiet, canEndAtMidnight: false)
                }
                Text(hours.quietIsOff ? "Wisp may alert you and work in the background at any time."
                                      : "No alerts and no background work, roughly while you sleep. Evenings still get reminders.")
                    .font(.system(size: 11)).foregroundStyle(ink.muted)
                    .fixedSize(horizontal: false, vertical: true)
                if showsOptions {
                    Button(hours.quietIsOff ? "Set quiet hours" : "I don't want quiet hours") {
                        hours.quiet = hours.quietIsOff ? UserHours.defaultQuiet : UserHours.Window(start: 0, end: 0)
                    }
                    .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(ink.muted)
                }
            }
            if showsOptions {
                Toggle("Offer to move an event that's outside my working hours", isOn: $hours.offerMoves)
                    .toggleStyle(.checkbox).font(.system(size: 12)).foregroundStyle(ink.label)
                    .help("At most one a day, only your own one-off events, and nothing moves until you confirm.")
            }
        }
    }

    private func row(_ title: String, window: Binding<UserHours.Window>, canEndAtMidnight: Bool) -> some View {
        HStack(spacing: 8) {
            Text(title).font(.system(size: 12.5, weight: .medium)).foregroundStyle(ink.label)
                .frame(width: 104, alignment: .leading)
            time(window.start, label: "\(title) start", upTo: 1410)
            Text("to").font(.system(size: 12)).foregroundStyle(ink.muted)
            time(window.end, label: "\(title) end", upTo: canEndAtMidnight ? 1440 : 1410)
            Spacer(minLength: 0)
        }
    }

    private func time(_ minute: Binding<Int>, label: String, upTo last: Int) -> some View {
        Menu {
            ForEach(UserHours.options(including: minute.wrappedValue, upTo: last), id: \.self) { option in
                Button(UserHours.clock(option)) { minute.wrappedValue = option }
            }
        } label: {
            Text(UserHours.clock(minute.wrappedValue))
                .font(.system(size: 12, weight: .medium).monospacedDigit()).foregroundStyle(ink.label)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Capsule().fill(ink.fill))
                .overlay(Capsule().stroke(ink.stroke, lineWidth: 1))
        }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
        .accessibilityLabel(label)
        .accessibilityValue(UserHours.clock(minute.wrappedValue))
    }

    private var days: some View {
        HStack(spacing: 4) {
            ForEach(UserHours.dayLetters, id: \.day) { entry in
                let on = hours.workingDays.contains(entry.day)
                Button {
                    if on { hours.workingDays.remove(entry.day) } else { hours.workingDays.insert(entry.day) }
                } label: {
                    Text(entry.letter).font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(on ? ink.onSelected : ink.muted)
                        .frame(width: 24, height: 22)
                        .background(Circle().fill(on ? ink.selected : ink.fill))
                        .overlay(Circle().stroke(on ? Color.clear : ink.stroke, lineWidth: 1))
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(entry.name)
                .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
            }
        }
    }
}

/// The Day tab's one offer. The service decides when; a look every quarter hour
/// is plenty, because it shows at most one a day.
@MainActor
final class HoursOfferModel: ObservableObject {
    @Published private(set) var offer: HoursMoveOffer?
    private var checkedAt: Date?
    private let client = WispClient()
    private static let checkEvery: TimeInterval = 15 * 60

    func refresh(now: Date = Date()) async {
        if let last = checkedAt, now.timeIntervalSince(last) < Self.checkEvery { return }
        checkedAt = now
        let reply = await client.hoursOffer()
        if reply.answered { offer = reply.offer }
    }

    /// Keep it where it is: the service won't ask about this event again.
    func keep() async {
        guard let offer else { return }
        self.offer = nil
        await client.finishHoursOffer(offer.id, accepted: false)
    }

    /// Taken to Ask, whose confirmation card decides. Nothing has moved yet.
    func taken() async {
        guard let offer else { return }
        self.offer = nil
        await client.finishHoursOffer(offer.id, accepted: true)
    }
}

struct HoursOfferRow: View {
    let offer: HoursMoveOffer
    let timezone: TimeZone
    var clock: (Double) -> String
    var isToday: (Double) -> Bool
    var onMove: (String) -> Void
    var onKeep: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(offer.line(eventTime: eventTime)).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                .lineLimit(3).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            NotchChrome.small("Move it") {
                onMove(offer.moveRequest(dayLabel: dayLabel, clock: clock))
            }
            .help("Opens Ask with this move. Wisp confirms before the calendar changes.")
            NotchChrome.small("Keep it") { onKeep() }
                .help("Leave it where it is. Wisp won't ask about this event again.")
        }
        .accessibilityElement(children: .contain)
    }

    private var eventTime: String {
        isToday(offer.whenTs) ? clock(offer.whenTs) : "\(weekday(offer.whenTs)) \(clock(offer.whenTs))"
    }

    private func weekday(_ timestamp: Double) -> String {
        Date(timeIntervalSince1970: timestamp).formatted(Date.FormatStyle(timeZone: timezone).weekday(.abbreviated))
    }

    private func dayLabel(_ timestamp: Double) -> String {
        Date(timeIntervalSince1970: timestamp).formatted(Date.FormatStyle(timeZone: timezone).weekday(.wide).month(.abbreviated).day())
    }
}
