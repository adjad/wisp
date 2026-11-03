import Foundation

// Pure checks for the read-aloud voice (app/Sources/WispApp/ReadAloudVoice.swift):
// the automatic male default, a removed voice falling back, the picker's list
// and the rate. No speech engine; the voices are made up.
@main
struct ReadAloudVoiceChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    typealias V = ReadAloudVoice.Candidate

    static func voice(_ name: String, _ language: String, _ quality: Int, _ gender: ReadAloudVoice.Gender,
                      id: String? = nil) -> V {
        V(id: id ?? "com.apple.voice.\(quality).\(language).\(name)", name: name, language: language,
          quality: quality, gender: gender)
    }

    static func main() {
        // This Mac on 2026-11-02: Samantha (female), Eloquence voices with no
        // gender, Fred (male but a novelty voice), Daniel en-GB male.
        let fred = voice("Fred", "en-US", 0, .male, id: "com.apple.speech.synthesis.voice.Fred")
        let daniel = voice("Daniel", "en-GB", 0, .male)
        let reed = voice("Reed", "en-US", 0, .unspecified, id: "com.apple.eloquence.en-US.Reed")
        let shelley = voice("Shelley", "en-US", 0, .unspecified, id: "com.apple.eloquence.en-US.Shelley")
        let samantha = voice("Samantha", "en-US", 0, .female)
        let mac = [samantha, reed, shelley, fred, daniel]
        check(fred.isNovelty && !daniel.isNovelty && !reed.isNovelty, "Old synthesis voices are novelty")
        check(ReadAloudVoice.automatic(mac) == daniel, "A male voice wins; never a novelty voice")
        check(ReadAloudVoice.resolve(saved: nil, among: mac) == .voice(daniel.id), "Unset: the automatic voice")

        // Quality first, then en-US, then other English.
        let premiumGB = voice("Jamie", "en-GB", 2, .male)
        let enhancedUS = voice("Evan", "en-US", 1, .male)
        let defaultUS = voice("Tom", "en-US", 0, .male)
        check(ReadAloudVoice.automatic([defaultUS, enhancedUS, premiumGB]) == premiumGB, "Premium beats en-US")
        check(ReadAloudVoice.automatic([defaultUS, enhancedUS, daniel]) == enhancedUS, "Enhanced beats default")
        check(ReadAloudVoice.automatic([daniel, defaultUS]) == defaultUS, "en-US beats other English at one quality")
        check(ReadAloudVoice.automatic([voice("Zoe", "en-US", 2, .female), daniel]) == daniel,
              "A female voice is never the male default")
        check(ReadAloudVoice.automatic([voice("Thomas", "fr-FR", 2, .male), daniel]) == daniel, "English only")

        // No gender reported: a known male name; otherwise the system voice.
        check(ReadAloudVoice.automatic([samantha, shelley, reed]) == reed, "Known male name when gender is missing")
        check(ReadAloudVoice.automatic([samantha, shelley]) == nil, "No male voice: none")
        check(ReadAloudVoice.resolve(saved: nil, among: [samantha]) == .system, "No male voice: the system voice")
        check(ReadAloudVoice.resolve(saved: nil, among: []) == .system, "No voices at all: the system voice")
        check(reed.genderLabel == "Male" && shelley.genderLabel == nil && samantha.genderLabel == "Female",
              "Gender labels")

        // Saved choices.
        check(ReadAloudVoice.resolve(saved: ReadAloudVoice.systemChoice, among: mac) == .system, "System voice chosen")
        check(ReadAloudVoice.resolve(saved: samantha.id, among: mac) == .voice(samantha.id), "A chosen voice speaks")
        check(ReadAloudVoice.resolve(saved: "com.apple.voice.premium.en-US.Gone", among: mac) == .voice(daniel.id),
              "A removed voice falls back to the automatic one")
        check(ReadAloudVoice.resolve(saved: "gone", among: [samantha]) == .system,
              "A removed voice with no male voice left: the system voice")
        check(ReadAloudVoice.resolve(saved: "", among: mac) == .voice(daniel.id), "Empty is unset")

        // The picker: the person's language and English, novelty last, best first.
        let french = voice("Thomas", "fr-FR", 1, .male)
        let german = voice("Anna", "de-DE", 1, .female)
        let listed = ReadAloudVoice.listed(mac + [french, german, premiumGB], preferredLanguage: "fr-CA")
        check(listed.first == premiumGB, "Best quality first")
        check(listed.contains(french) && !listed.contains(german), "Own language and English only")
        check(listed.last == fred, "Novelty voices last")
        check(!ReadAloudVoice.listed(mac + [french], preferredLanguage: "en-US").contains(french), "English only for en")
        check(premiumGB.qualityLabel == "Premium" && enhancedUS.qualityLabel == "Enhanced"
              && daniel.qualityLabel == "Default", "Quality labels")

        // Rate: unset or odd values read as normal; kept within range.
        check(ReadAloudVoice.rateMultiple(nil) == 1, "Unset rate is normal")
        check(ReadAloudVoice.rateMultiple(.nan) == 1 && ReadAloudVoice.rateMultiple(0) == 1, "Bad rate is normal")
        check(ReadAloudVoice.rateMultiple(9) == ReadAloudVoice.rateRange.upperBound, "Rate capped")
        check(ReadAloudVoice.rateMultiple(0.1) == ReadAloudVoice.rateRange.lowerBound, "Rate floored")
        check(ReadAloudVoice.rateMultiple(1.25) == 1.25, "Rate kept")

        print("read-aloud voice checks passed")
    }
}
