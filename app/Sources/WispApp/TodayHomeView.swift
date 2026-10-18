import SwiftUI

/// The Wisp window's Today section: the full planner (both tabs, the task
/// editor, pins, working hours and the daily email) under the window's top bar.
/// It shares the app's one TodayModel with the notch's Brief and Day tabs.
struct TodayHomeView: View {
    @ObservedObject var today: TodayModel
    let initialTab: Int
    @Binding var sidebarVisible: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                if !sidebarVisible { Spacer().frame(width: 62) }   // clear the window's traffic lights
                Button { sidebarVisible.toggle() } label: { Image(systemName: "sidebar.left") }
                    .buttonStyle(ChatIconButtonStyle()).help("Show or hide the sidebar (⌘B)").accessibilityLabel("Toggle sidebar")
                Text("Today").font(.system(size: 14.5, weight: .medium)).foregroundStyle(ChatTheme.text)
                Spacer()
            }
            .padding(.leading, 14).padding(.trailing, 16)
            .frame(height: 52)
            Rectangle().fill(ChatTheme.line).frame(height: 1)
            TodayView(model: today, initialTab: initialTab)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }
}
