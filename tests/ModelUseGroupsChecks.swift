import Foundation

/// ModelUseGroups: Settings › Models has two menus, General use and Coding. Every chat model the
/// engine lists is offered in both; the Coding menu lists Qwen 3.5 9B first when it is there; one
/// pick moves the menu's roles together, and a cloud or connected-app binding stays. Pure.
@main
struct ModelUseGroupsChecks {
    static func main() {
        var count = 0, failed = 0
        func check(_ c: Bool, _ m: String) { count += 1; if !c { failed += 1; print("FAIL: \(m)") } }
        typealias G = ModelUseGroups

        check(G.all.map(\.title) == ["General use", "Coding"], "two menus, General use then Coding")
        check(G.general.primary == "general" && Set(G.general.roles) == ["general", "fast", "reasoning"],
              "General use moves general (and agent with it), fast (and router) and reasoning")
        check(G.coding.roles == ["coding"], "Coding moves coding; research follows it unless it has its own")
        check(G.codingFirst == ["Qwen3.5-9B-oQ4e-mtp"], "the user's Qwen 3.5 9B is the Coding suggestion")

        // Invented roster: Wisp's default, two other installed models, the embedder and the reranker.
        let installed = ["Gemma-Wisp", "Alpha-7B-oQ4e", "Qwen3.5-9B-oQ4e-mtp",
                         "Qwen3-Embedding-0.6B-4bit-DWQ", "Qwen3-Reranker-0.6B-mlx-6bit"]
        let roles = ["general": "Gemma-Wisp", "coding": "Gemma-Wisp",
                     "embedding": "Qwen3-Embedding-0.6B-4bit-DWQ", "reranker": "Qwen3-Reranker-0.6B-mlx-6bit"]
        let general = G.choices(G.general, installed: installed, roles: roles, keeping: "Gemma-Wisp")
        let coding = G.choices(G.coding, installed: installed, roles: roles, keeping: "Gemma-Wisp")
        check(Set(general) == ["Gemma-Wisp", "Alpha-7B-oQ4e", "Qwen3.5-9B-oQ4e-mtp"],
              "General use offers every chat model on the Mac, not the embedder or reranker: \(general)")
        check(Set(coding) == Set(general), "Coding offers the same models")
        check(coding.first == "Qwen3.5-9B-oQ4e-mtp", "Qwen 3.5 9B is first in Coding: \(coding)")
        check(general == general.sorted(), "General use stays sorted")
        check(Array(coding.dropFirst()) == coding.dropFirst().sorted(), "the rest of Coding stays sorted")

        let noQwen = G.choices(G.coding, installed: ["Gemma-Wisp", "Alpha-7B-oQ4e"], roles: roles)
        check(!noQwen.contains("Qwen3.5-9B-oQ4e-mtp"), "a Mac without Qwen is never offered it")
        let saved = G.choices(G.coding, installed: ["Gemma-Wisp"], roles: ["coding": "Beta-3B"], keeping: "Beta-3B")
        check(saved.contains("Beta-3B"), "the saved pick is always listed")
        check(G.choices(G.general, installed: [], roles: [:]) == [RoleModelChoices.bundledDefault],
              "nothing reported yet: only the bundled default")

        check(G.assignments(G.general, model: "Alpha-7B-oQ4e")
              == ["general": "Alpha-7B-oQ4e", "fast": "Alpha-7B-oQ4e", "reasoning": "Alpha-7B-oQ4e"],
              "one General pick moves all three roles")
        check(G.assignments(G.general, model: "Alpha-7B-oQ4e", keep: ["reasoning"])
              == ["general": "Alpha-7B-oQ4e", "fast": "Alpha-7B-oQ4e"],
              "a reasoning role on the cloud or a connected app keeps its binding")
        check(G.assignments(G.coding, model: "Qwen3.5-9B-oQ4e-mtp") == ["coding": "Qwen3.5-9B-oQ4e-mtp"],
              "a Coding pick moves coding")
        check(G.assignments(G.general, model: "M", keep: ["general"])["general"] == "M",
              "the menu's own role always moves")

        print("ModelUseGroupsChecks: \(count) checks, \(failed) failed")
        exit(failed == 0 ? 0 : 1)
    }
}
