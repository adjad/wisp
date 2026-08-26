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
    private let store = CNContactStore()
    private var timer: Timer?

    func start() {
        // Contacts change rarely, so this is a cheap once-at-launch read plus
        // a slow refresh — no need for the retry ramp the volatile sources use,
        // beyond one warm-up in case the backend isn't listening yet.
        sync()
        DispatchQueue.main.asyncAfter(deadline: .now() + 8.0) { [weak self] in self?.sync() }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 21600, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    func sync() {
        store.requestAccess(for: .contacts) { [weak self] granted, _ in
            guard granted, let self else { return }
            DispatchQueue.global(qos: .utility).async { self.read() }
        }
    }

    private func read() {
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
        do {
            try store.enumerateContacts(with: req) { c, _ in
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
        } catch {
            return   // access revoked mid-read, or store unavailable
        }
        guard !map.isEmpty else { return }
        post(contacts: map, birthdays: birthdays)
    }

    private func post(contacts: [String: String], birthdays: [String: [String]]) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/messages")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "contacts": contacts, "birthdays": birthdays,
        ])
        URLSession.shared.dataTask(with: req).resume()
    }
}
