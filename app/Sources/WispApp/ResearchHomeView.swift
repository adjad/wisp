import SwiftUI

/// The Wisp window's Research section: the library under the window's top bar.
struct ResearchHomeView: View {
    @ObservedObject var library: ResearchLibraryModel
    let launchers: ChatLaunchers
    @Binding var sidebarVisible: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                if !sidebarVisible { Spacer().frame(width: 62) }   // clear the window's traffic lights
                Button { sidebarVisible.toggle() } label: { Image(systemName: "sidebar.left") }
                    .buttonStyle(ChatIconButtonStyle()).help("Show or hide the sidebar (⌘B)").accessibilityLabel("Toggle sidebar")
                Text("Research").font(.system(size: 14.5, weight: .medium)).foregroundStyle(ChatTheme.text)
                Spacer()
            }
            .padding(.leading, 14).padding(.trailing, 16)
            .frame(height: 52)
            Rectangle().fill(ChatTheme.line).frame(height: 1)
            ResearchLibraryView(model: library, onOpen: launchers.openResearchJob, onNew: launchers.newResearch)
        }
        .frame(maxWidth: .infinity)
    }
}
