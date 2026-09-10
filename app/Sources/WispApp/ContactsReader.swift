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
         }, snapshot: (() throws -> Snapshot)? = nil) {
        self.channel = channel
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
        var map: [String: String] = [:]
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
            for p in c.phoneNumbers { map[p.value.stringValue] = name }
            for e in c.emailAddresses { map[e.value as String] = name }
            if let bday = c.birthday, let m = bday.month, let d = bday.day {
                let key = String(format: "%02d-%02d", m, d)
                birthdays[key, default: []].append(name)
            }
        }
        return (contacts: map, birthdays: birthdays)
    }
}
