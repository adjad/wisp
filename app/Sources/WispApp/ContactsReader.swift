import AppKit
import Contacts
import Foundation

// Maps phone numbers / email handles to contact NAMES and pushes the map to
// the backend, so Messages content stops being anonymous.
//
// chat.db stores only handles ("+19255576442"), never names — so before this
// existed, everything downstream saw phone numbers. The profile builder in
// particular had no way to tell who anyone was and guessed at relationships,
// which is exactly how it ended up naming the wrong person as the user's
// sister. Resolving handles to real names removes the guesswork.
//
// Lives in the Swift app for the same reason as the other readers: Contacts
// (TCC) is per-process, so the prompt reads "Wisp wants to access Contacts"
// and works once granted, unlike the separate Python backend.
final class ContactsReader {
    static let enabledKey = "wisp.contactsEnabled"
    static let preferenceChanged = Notification.Name("wisp.contactsPreferenceChanged")
    static let delivery = PrivacySyncChannel(key: "wisp.contactsRevision",
                                             endpoint: "assistant/sync/messages")
    typealias Snapshot = (contacts: [String: String], birthdays: [String: [String]])
    private let store = CNContactStore()
    private let channel: PrivacySyncChannel
    private let enabled: () -> Bool
    private let authorization: () -> CNAuthorizationStatus
    private let snapshot: (() throws -> Snapshot)?
    // Whether asking macOS for Contacts would be worth it yet. Names are for
    // Messages, so Wisp asks once Messages can be read, not at launch.
    private let mayAsk: () -> Bool
    private var timer: Timer?
    private var permissionTimer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var lastAuthorization: CNAuthorizationStatus?
    private var lastEnabled: Bool?

    init(channel: PrivacySyncChannel = ContactsReader.delivery,
         enabled: @escaping () -> Bool = {
             UserDefaults.standard.object(forKey: enabledKey) as? Bool ?? true
         },
         authorization: @escaping () -> CNAuthorizationStatus = {
             CNContactStore.authorizationStatus(for: .contacts)
         }, snapshot: (() throws -> Snapshot)? = nil,
         mayAsk: @escaping () -> Bool = { true }) {
        self.channel = channel
        self.mayAsk = mayAsk
        self.enabled = enabled
        self.authorization = authorization
        self.snapshot = snapshot
        channel.refresh = { [weak self] in self?.sync() }
        for name in [Self.preferenceChanged, .CNContactStoreDidChange,
                     NSApplication.didBecomeActiveNotification] {
            observers.append(NotificationCenter.default.addObserver(forName: name, object: nil,
                                                                     queue: .main) { [weak self] _ in
                self?.sync()
            })
        }
    }

    deinit {
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        timer?.invalidate()
        permissionTimer?.invalidate()
    }

    static func setEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: enabledKey)
        NotificationCenter.default.post(name: preferenceChanged, object: nil)
    }

    private static func canRead(_ status: CNAuthorizationStatus) -> Bool {
        status == .authorized
    }

    /// Ask only while macOS has no answer and asking is worth it now.
    static func shouldRequest(_ status: CNAuthorizationStatus, mayAsk: Bool) -> Bool {
        status == .notDetermined && mayAsk
    }

    func start() {
        channel.startMonitoring()
        sync()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.timer = Timer.scheduledTimer(withTimeInterval: 21600, repeats: true) { [weak self] _ in
                self?.sync()
            }
            // TCC does not guarantee a store-change notification for revocation.
            // Poll only permission state, never contacts, and also check on focus.
            self.permissionTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
                self?.checkAccess()
            }
        }
    }

    func checkAccess() {
        if authorization() != lastAuthorization || enabled() != lastEnabled { sync() }
    }

    func sync() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let revision = self.channel.begin()
            self.lastAuthorization = self.authorization()
            self.lastEnabled = self.enabled()
            guard self.enabled() else {
                self.postClear(revision: revision)
                return
            }
            if self.lastAuthorization == .notDetermined {
                // Clear old persisted recipients before prompting for access.
                self.postClear(revision: revision)
                // Not yet worth a prompt: a later sync (focus, the 6 h timer, a
                // backend refresh) asks once it is.
                guard Self.shouldRequest(.notDetermined, mayAsk: self.mayAsk()) else { return }
                self.store.requestAccess(for: .contacts) { [weak self] _, _ in
                    // Recheck TCC instead of trusting a now-stale callback.
                    if let self, self.authorization() != .notDetermined { self.sync() }
                }
                return
            }
            guard Self.canRead(self.authorization()) else {
                self.postClear(revision: revision)
                return
            }
            DispatchQueue.global(qos: .utility).async { [weak self] in
                guard let self else { return }
                let result = try? (self.snapshot?() ?? self.read())
                DispatchQueue.main.async {
                    guard self.channel.revision == revision else { return }
                    guard self.enabled(), Self.canRead(self.authorization()), let result else {
                        self.postClear(revision: revision)
                        return
                    }
                    // Empty is authoritative, including contacts with birthdays
                    // but no phone/email handles. Never silently keep old names.
                    self.channel.submit(["contacts_enabled": true, "contacts_available": true,
                                         "contacts": result.contacts, "birthdays": result.birthdays],
                                        revision: revision)
                }
            }
        }
    }

    private func postClear(revision: Int) {
        channel.submit(["contacts_enabled": enabled(), "contacts_available": false,
                        "contacts": [String: String](), "birthdays": [String: [String]]()],
                       revision: revision)
    }

    private func read() throws -> Snapshot {
        let keys: [CNKeyDescriptor] = [
            CNContactGivenNameKey as CNKeyDescriptor,
            CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactNicknameKey as CNKeyDescriptor,
            CNContactOrganizationNameKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
            CNContactBirthdayKey as CNKeyDescriptor,
        ]
        var cards: [Card] = []
        // month-day ("MM-DD") -> name, year omitted deliberately: most saved
        // birthdays in Contacts have no year (CNContact leaves it nil when the
        // user only entered month/day), and contact_dates only needs "how many
        // days until" — the exact age is neither reliably available nor asked
        // for. Two people sharing a day is fine; names are comma-joined by the
        // reader on the Python side rather than colliding here.
        var birthdays: [String: [String]] = [:]
        let req = CNContactFetchRequest(keysToFetch: keys)
        try store.enumerateContacts(with: req) { c, stop in
            guard self.enabled() else { stop.pointee = true; return }
            let full = [c.givenName, c.familyName]
                .filter { !$0.isEmpty }.joined(separator: " ")
            // Fall back to nickname, then organization — a contact saved
            // only as a company ("Sprouts") is still far more useful
            // downstream than a bare phone number.
            let name = !full.isEmpty ? full
                : (!c.nickname.isEmpty ? c.nickname : c.organizationName)
            guard !name.isEmpty else { return }
            cards.append((name: name,
                          phones: c.phoneNumbers.map { $0.value.stringValue },
                          emails: c.emailAddresses.map { $0.value as String }))
            if let bday = c.birthday, let m = bday.month, let d = bday.day {
                let key = String(format: "%02d-%02d", m, d)
                birthdays[key, default: []].append(name)
            }
        }
        return (contacts: Self.handleMap(cards), birthdays: birthdays)
    }

    /// One contact card as read: its display name and its phone/email handles.
    typealias Card = (name: String, phones: [String], emails: [String])

    /// The handle -> name map pushed to the backend.
    ///
    /// The backend groups handles by the lowercased name, so two different
    /// people saved under one name ("Chris" twice) would become one contact
    /// there, and a text to "Chris" would pick a number without asking. Those
    /// cards get a short label instead ("Chris (…0142)"), so the backend sees
    /// two contacts and asks which one. Cards that share a handle are one
    /// person saved twice and keep the plain name.
    static func handleMap(_ cards: [Card]) -> [String: String] {
        var people: [String: [(handles: Set<String>, members: [Int])]] = [:]
        for (index, card) in cards.enumerated() {
            let handles = Set((card.phones + card.emails).map { normalizedHandle($0) }.filter { !$0.isEmpty })
            guard !handles.isEmpty else { continue }
            let key = card.name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            var groups = people[key] ?? []
            // A card sharing a number or address with another card of that
            // name is the same person saved twice: one handle can carry only
            // one name, so it could not tell them apart anyway.
            var merged: (handles: Set<String>, members: [Int]) = (handles: handles, members: [index])
            for match in groups.indices.reversed() where !groups[match].handles.isDisjoint(with: handles) {
                merged.handles.formUnion(groups[match].handles)
                merged.members.append(contentsOf: groups[match].members)
                groups.remove(at: match)
            }
            merged.members.sort()
            groups.append(merged)
            people[key] = groups
        }
        var labels: [Int: String] = [:]
        for key in people.keys.sorted() {
            guard let groups = people[key], groups.count > 1 else { continue }
            var taken = Set<String>()
            for group in groups {
                let first = cards[group.members[0]]
                let hint = distinguisher(first)
                var label = "\(first.name) (\(hint))"
                var count = 2
                while taken.contains(label.lowercased()) {
                    label = "\(first.name) (\(hint) \(count))"
                    count += 1
                }
                taken.insert(label.lowercased())
                for member in group.members { labels[member] = label }
            }
        }
        var map: [String: String] = [:]
        for (index, card) in cards.enumerated() {
            let name = labels[index] ?? card.name
            for phone in card.phones { map[phone] = name }
            for email in card.emails { map[email] = name }
        }
        return map
    }

    /// The same key the backend uses: the last 10 digits of a phone, or a
    /// lowercased email address.
    static func normalizedHandle(_ handle: String) -> String {
        let trimmed = handle.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.contains("@") { return trimmed.lowercased() }
        let digits = String(trimmed.filter { $0.isASCII && $0.isNumber })
        return digits.count >= 10 ? String(digits.suffix(10)) : digits
    }

    /// What tells one same-named card from another: the last four digits of
    /// its first phone, else its first email address.
    private static func distinguisher(_ card: Card) -> String {
        for phone in card.phones {
            let digits = normalizedHandle(phone)
            if !digits.isEmpty { return "…" + String(digits.suffix(4)) }
        }
        let emails = card.emails.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        return emails.first(where: { !$0.isEmpty }) ?? ""
    }
}
