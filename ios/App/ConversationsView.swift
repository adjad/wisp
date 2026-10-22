import SwiftUI

struct ConversationsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                if model.conversations.isEmpty {
                    Text("Your chats will appear here.").foregroundStyle(.secondary)
                }
                ForEach(model.conversations) { conversation in
                    Button {
                        model.open(conversation)
                        dismiss()
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: conversation.engine.symbol)
                                .foregroundStyle(Theme.teal)
                                .frame(width: 22)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(conversation.title).lineLimit(1)
                                Text(conversation.updatedAt, format: .relative(presentation: .named))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .listRowBackground(conversation.id == model.currentID ? Theme.indigo.opacity(0.25) : nil)
                }
                .onDelete { offsets in
                    model.delete(Set(offsets.map { model.conversations[$0].id }))
                }
            }
            .navigationTitle("Chats")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        model.newChat()
                        dismiss()
                    } label: { Label("New chat", systemImage: "square.and.pencil") }
                }
            }
        }
    }
}
