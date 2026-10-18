import SwiftUI

/// The Wisp window's Memory section: what Wisp knows about you and where it
/// learned it, in one place. Facts and Review are the saved-memory store
/// (`/memory/*`); Routines are what the behavior engine learned (`/behavior/*`).
/// The merge is only here, in the UI: both stores and their rules are unchanged.
struct MemoryHomeView: View {
    @ObservedObject var navigation: WispWindowNavigation
    @Binding var sidebarVisible: Bool
    @State private var unreviewed = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            topBar
            Rectangle().fill(ChatTheme.line).frame(height: 1)
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Memory").font(.system(size: 22, weight: .medium)).foregroundStyle(ChatTheme.text)
                    Text("What Wisp knows about you, and where it learned it.")
                        .font(.system(size: 13)).foregroundStyle(ChatTheme.text3)
                }
                Picker("Memory", selection: $navigation.memoryTab) {
                    ForEach(WispRoute.MemoryFocus.allCases, id: \.self) { tab in
                        Text(tab == .review && unreviewed > 0 ? "Review · \(unreviewed)" : tab.title).tag(tab)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                // Each tab keeps its own model; switching starts it fresh.
                Group {
                    switch navigation.memoryTab {
                    case .facts: MemoryView(scope: .facts)
                    case .routines: LearnedView(model: navigation.learned)
                    case .about: AboutYouView()
                    case .review: MemoryView(scope: .review)
                    }
                }
                .id(navigation.memoryTab)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
            .padding(.horizontal, 24).padding(.top, 18)
        }
        .frame(maxWidth: .infinity)
        .task {
            while !Task.isCancelled {
                unreviewed = await MemoryModel.unreviewedCount() ?? unreviewed
                do { try await Task.sleep(for: .seconds(15)) } catch { break }
            }
        }
    }

    private var topBar: some View {
        HStack(spacing: 8) {
            if !sidebarVisible { Spacer().frame(width: 62) }   // clear the window's traffic lights
            Button { sidebarVisible.toggle() } label: { Image(systemName: "sidebar.left") }
                .buttonStyle(ChatIconButtonStyle()).help("Show or hide the sidebar (⌘B)").accessibilityLabel("Toggle sidebar")
            Text("Memory").font(.system(size: 14.5, weight: .medium)).foregroundStyle(ChatTheme.text)
            Spacer()
            ChatPill(text: "Local", tinted: true)
        }
        .padding(.leading, 14).padding(.trailing, 16)
        .frame(height: 52)
    }
}
