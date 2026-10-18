import SwiftUI
import AppKit

struct ResearchView: View {
    @ObservedObject var model: ResearchModel
    /// Compact mode is the notch's Research tab, where every research job is
    /// shown. The full-size layout has no host in the app.
    var compact = false
    var onBack: (() -> Void)? = nil
    /// First tap on the trash arms this; a second tap within a few seconds deletes.
    @State private var confirmingDelete = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().overlay(Theme.hairline)
            content
                .disabled(model.actionInProgress)
            if !model.errorText.isEmpty && model.phase != .error && model.phase != .awaitingApproval {
                Text(model.errorText).font(.callout).foregroundStyle(Theme.bad).padding(14)
            }
        }
        .frame(minWidth: compact ? nil : 720, minHeight: compact ? nil : 560)
        .background(compact ? Color.clear : Theme.surface)
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        HStack(spacing: 10) {
            if compact, let onBack {
                Button(action: onBack) {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 12, weight: .bold))
                }.buttonStyle(.borderless).help("Back").accessibilityLabel("Back")
            }
            Image(systemName: "binoculars.fill")
                .font(.system(size: 17)).foregroundStyle(Theme.textPrimary)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.title.isEmpty ? "Wisp Research" : model.title)
                    .font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                Text(model.status.isEmpty ? "Local, evidence-backed research" : model.status)
                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            Spacer()
            if !model.elapsedText.isEmpty && model.isRunning {
                chip(model.elapsedText, icon: "clock")
            }
            if model.evidenceCount > 0 {
                chip("\(model.evidenceCount) evidence", icon: "checkmark.seal")
            }
            if !model.sources.isEmpty {
                chip("\(model.sources.filter { $0.status == "read" }.count) read",
                     icon: "doc.text.magnifyingglass")
            }
            if !model.jobId.isEmpty && model.phase != .idle && model.phase != .planning {
                let pinHelp = model.pinned ? "Pinned — kept from automatic 30-day cleanup"
                    : "Pin to keep this job's data indefinitely"
                if compact {
                    NotchChrome.iconButton(model.pinned ? "pin.fill" : "pin", help: pinHelp, action: model.togglePin)
                        .disabled(model.actionInProgress)
                } else {
                    Button(action: model.togglePin) {
                        Image(systemName: model.pinned ? "pin.fill" : "pin")
                    }.buttonStyle(.borderless).disabled(model.actionInProgress).help(pinHelp)
                        .accessibilityLabel(model.pinned ? "Unpin research job" : "Pin research job")
                }
                deleteControl
            }
            if model.isFinished {
                if compact {
                    NotchChrome.chip("Export", icon: "square.and.arrow.down", help: "Export this report as Markdown",
                                     action: model.export)
                } else {
                    Button(action: model.export) {
                        Label("Export Markdown", systemImage: "square.and.arrow.down")
                            .font(.system(size: 11, weight: .medium))
                    }.buttonStyle(.bordered)
                }
            }
        }
        .padding(.horizontal, 20).padding(.vertical, 14)
    }

    private func performDelete() {
        confirmingDelete = false
        model.deleteJob {
            if let onBack { onBack() } else { NSApp.keyWindow?.close() }
        }
    }

    /// An inline two-step delete: the notch panel can't host a confirmation dialog.
    @ViewBuilder private var deleteControl: some View {
        if confirmingDelete {
            HStack(spacing: 6) {
                Text("Delete?").font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.bad)
                if compact {
                    NotchChrome.small("Confirm", action: performDelete)
                        .disabled(model.actionInProgress)
                        .accessibilityLabel("Confirm delete research job")
                    NotchChrome.small("Keep") { confirmingDelete = false }
                        .accessibilityLabel("Keep research job")
                } else {
                    Button("Confirm", role: .destructive, action: performDelete)
                        .buttonStyle(.bordered).disabled(model.actionInProgress)
                    Button("Keep") { confirmingDelete = false }.buttonStyle(.bordered)
                }
            }
            .task(id: confirmingDelete) {
                // Reverts on its own so a stray first tap never stays armed.
                do { try await Task.sleep(for: .seconds(4)) } catch { return }
                confirmingDelete = false
            }
        } else {
            Button { confirmingDelete = true } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless).disabled(model.actionInProgress)
            .help("Delete this research job and all its data")
            .accessibilityLabel("Delete research job")
        }
    }

    @ViewBuilder private var content: some View {
        Group {
            switch model.phase {
            case .idle, .planning:
                centeredProgress(model.status.isEmpty ? "Preparing…" : model.status)
            case .awaitingApproval:
                planEditor
            case .running, .paused:
                if compact { inlineRunningView } else { runningView }
            case .complete, .partial:
                if compact { inlineReportView } else { reportView }
            case .cancelled:
                notice("Research cancelled", detail: "Sources and evidence gathered so far remain in Wisp's local research database.")
            case .error:
                VStack {
                    notice("Research couldn't finish", detail: model.errorText)
                    if !model.jobId.isEmpty {
                        if compact {
                            NotchChrome.chip("Refresh saved job", icon: "arrow.clockwise", help: "Reload this job from disk",
                                             action: model.refreshSavedJob).padding(.bottom, 16)
                        } else {
                            Button("Refresh saved job", action: model.refreshSavedJob).padding(.bottom, 16)
                        }
                    }
                }
            }
        }
        .frame(maxHeight: compact ? 500 : nil)
    }

    private var planEditor: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Review the plan").font(.system(size: compact ? 16 : 21, weight: .semibold))
                    Spacer()
                    Picker("Depth", selection: $model.depth) {
                        Text("Quick").tag("quick")
                        Text("Standard").tag("standard")
                        Text("Deep").tag("deep")
                    }.pickerStyle(.segmented).frame(width: compact ? 220 : 260)
                }
                labeled("Report title") {
                    TextField("Research title", text: $model.title).modifier(AdaptiveField(compact: compact))
                }
                labeled("Objective") {
                    TextEditor(text: $model.objective)
                        .font(.system(size: 14)).frame(minHeight: 78)
                        .padding(6).background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke))
                }
                labeled("Questions Wisp will investigate") {
                    VStack(spacing: 8) {
                        ForEach(model.subquestions.indices, id: \.self) { i in
                            HStack(alignment: .top, spacing: 8) {
                                Text("\(i + 1)").font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(Theme.textMuted).frame(width: 18, height: 26)
                                TextField("Subquestion", text: $model.subquestions[i], axis: .vertical)
                                    .modifier(AdaptiveField(compact: compact))
                                if model.subquestions.count > 2 {
                                    Button { model.subquestions.remove(at: i) } label: {
                                        Image(systemName: "minus.circle").foregroundStyle(Theme.textMuted)
                                    }.buttonStyle(.plain)
                                }
                            }
                        }
                        Button { model.subquestions.append("") } label: {
                            Label("Add question", systemImage: "plus")
                        }.buttonStyle(.plain).foregroundStyle(Theme.textSecondary)
                    }
                }
                labeled("Sources (optional)") {
                    HStack(alignment: .top, spacing: 12) {
                        labeledSmall("Only search these domains") {
                            TextField("blank = open web", text: $model.allowedDomainsText)
                                .modifier(AdaptiveField(compact: compact))
                        }
                        labeledSmall("Never search these domains") {
                            TextField("e.g. pinterest.com", text: $model.blockedDomainsText)
                                .modifier(AdaptiveField(compact: compact))
                        }
                    }
                }
                if !model.errorText.isEmpty {
                    Text(model.errorText).font(.system(size: 12)).foregroundStyle(Theme.bad)
                }
                HStack {
                    Text("Search queries and page requests leave your Mac; model inference and the research database stay local.")
                        .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                    Spacer()
                    if compact {
                        NotchChrome.chip("Start research", icon: "arrow.right.circle.fill",
                                         help: "Start researching this plan", action: model.start)
                    } else {
                        Button(action: model.start) {
                            Label("Start research", systemImage: "arrow.right.circle.fill")
                                .font(.system(size: 13, weight: .semibold))
                        }.buttonStyle(.borderedProminent)
                    }
                }
            }.padding(compact ? 18 : 24)
        }
    }

    private var runningView: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    ProgressView().controlSize(.small)
                    Text(model.status).font(.system(size: 13, weight: .medium))
                    Spacer()
                    Button(model.paused ? "Resume" : "Pause", action: model.togglePause)
                        .buttonStyle(.bordered)
                    Button("Cancel", role: .destructive, action: model.cancel)
                        .buttonStyle(.bordered)
                }
                Divider().overlay(Theme.hairline)
                Text("Activity").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 7) {
                        ForEach(Array(model.activity.enumerated()), id: \.offset) { _, item in
                            Text(item).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                DisclosureGroup("Source domains") {
                    VStack(alignment: .leading, spacing: 6) {
                        labeledSmall("Only search these domains (comma-separated, blank = any)") {
                            TextField("e.g. nature.com, arxiv.org", text: $model.allowedDomainsText)
                                .modifier(AdaptiveField(compact: compact)).onSubmit(model.updateDomains)
                        }
                        labeledSmall("Never search these domains") {
                            TextField("e.g. pinterest.com", text: $model.blockedDomainsText)
                                .modifier(AdaptiveField(compact: compact)).onSubmit(model.updateDomains)
                        }
                        HStack {
                            Spacer()
                            Button("Apply from next round", action: model.updateDomains)
                                .buttonStyle(.bordered).font(.system(size: 11))
                        }
                    }.padding(.top, 6)
                }.font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                HStack {
                    TextField("Steer the research…", text: $model.steering)
                        .modifier(AdaptiveField(compact: compact)).onSubmit(model.sendSteering)
                    Button("Send", action: model.sendSteering).disabled(model.steering.isEmpty)
                }
            }.padding(18).frame(minWidth: 390)

            sourceList.frame(minWidth: 280, idealWidth: 330).padding(18)
        }
    }

    private var reportView: some View {
        HSplitView {
            ScrollView {
                MarkdownView(text: model.report)
                    .padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }.frame(minWidth: 480)
            citationList.frame(minWidth: 300, idealWidth: 350).padding(18)
        }
    }

    /// A single-column research dashboard for Wisp's 640-point main panel.
    /// The full desktop version uses split panes; in the panel they would
    /// force horizontal scrolling and hide the actual research progress.
    private var inlineRunningView: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                ProgressView().controlSize(.small)
                Text(model.status).font(.system(size: 13, weight: .medium)).lineLimit(2)
                Spacer()
                NotchChrome.chip(model.paused ? "Resume" : "Pause", icon: model.paused ? "play.fill" : "pause.fill",
                                 help: model.paused ? "Resume research" : "Pause research", action: model.togglePause)
                NotchChrome.chip("Cancel", icon: "xmark", help: "Stop this research", action: model.cancel)
            }
            Divider().overlay(Theme.hairline)
            Text("Activity").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(model.activity.enumerated()), id: \.offset) { _, item in
                        Text(item).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }.frame(maxHeight: 105)
            if !model.sources.isEmpty {
                sourceList.frame(maxHeight: 145)
            }
            DisclosureGroup("Source domains") {
                VStack(alignment: .leading, spacing: 6) {
                    TextField("Only these domains (comma-separated)", text: $model.allowedDomainsText)
                        .modifier(NotchField()).onSubmit(model.updateDomains)
                    TextField("Never these domains", text: $model.blockedDomainsText)
                        .modifier(NotchField()).onSubmit(model.updateDomains)
                    HStack { Spacer(); NotchChrome.chip("Apply", icon: nil, help: "Apply from the next round", action: model.updateDomains) }
                }.padding(.top, 6)
            }.font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            HStack {
                TextField("Steer the research…", text: $model.steering)
                    .modifier(NotchField()).onSubmit(model.sendSteering)
                NotchChrome.chip("Send", icon: nil, help: "Send this to the research", action: model.sendSteering)
                    .disabled(model.steering.isEmpty)
            }
        }.padding(18).frame(maxHeight: 500)
    }

    private var inlineReportView: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                MarkdownView(text: model.report)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Divider().overlay(Theme.hairline)
                citationList.frame(height: 185)
            }.padding(18)
        }.frame(maxHeight: 500)
    }

    private var citationList: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Citation evidence")
                .font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            Text("Exact passages retained from the pages Wisp read. Click a card to open the source.")
                .font(.system(size: 10)).foregroundStyle(Theme.textMuted)
            if !model.contradictions.isEmpty {
                DisclosureGroup("Disagreements (\(model.contradictions.count))") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(model.contradictions) { row in
                            Text(row.description).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                        }
                    }.padding(.top, 4)
                }.font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.bad)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(model.citations) { citation in
                        Button { model.open(citation) } label: {
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Text("[\(citation.number)] \(citation.title)")
                                        .font(.system(size: 12, weight: .semibold)).lineLimit(2)
                                    if !citation.qualityClass.isEmpty {
                                        Spacer(minLength: 4)
                                        qualityBadge(citation.qualityClass, reason: citation.qualityReason)
                                    }
                                }
                                if !citation.claim.isEmpty && citation.claim != citation.quote {
                                    Text(citation.claim).font(.system(size: 11))
                                        .foregroundStyle(Theme.textSecondary)
                                }
                                Text("“\(citation.quote)”")
                                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                                    .lineLimit(8)
                                if !citation.publishedAt.isEmpty {
                                    Text(citation.publishedAt).font(.system(size: 10)).foregroundStyle(Theme.textMuted)
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
                                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke))
                        }.buttonStyle(.plain).disabled(citation.url.isEmpty)
                    }
                }
            }
        }
    }

    private func qualityBadge(_ qualityClass: String, reason: String = "") -> some View {
        Text(qualityClass.replacingOccurrences(of: "_", with: " "))
            .font(.system(size: 9, weight: .medium)).foregroundStyle(Theme.textMuted)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Capsule().fill(Theme.chipFill)).overlay(Capsule().stroke(Theme.chipStroke))
            .help(reason.isEmpty ? qualityClass.replacingOccurrences(of: "_", with: " ") : reason)
    }

    private var sourceList: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Sources").font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(model.sources) { source in
                        Button { model.open(source) } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack {
                                    Image(systemName: source.status == "failed" ? "exclamationmark.triangle" :
                                            (source.status == "read" || source.status == "extracted"
                                             ? "checkmark.circle.fill" : "circle"))
                                        .font(.system(size: 10))
                                    Text(source.title).font(.system(size: 12, weight: .medium)).lineLimit(2)
                                    if !source.qualityClass.isEmpty {
                                        Spacer(minLength: 4)
                                        qualityBadge(source.qualityClass, reason: source.qualityReason)
                                    }
                                }
                                Text(source.domain).font(.system(size: 10)).foregroundStyle(Theme.textMuted)
                                if !source.error.isEmpty {
                                    Text(source.error).font(.system(size: 10)).foregroundStyle(Theme.bad).lineLimit(2)
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(9)
                                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke))
                        }.buttonStyle(.plain).disabled(source.url.isEmpty)
                    }
                }
            }
        }
    }

    private func centeredProgress(_ text: String) -> some View {
        VStack(spacing: 12) {
            ProgressView().controlSize(.large)
            Text(text).font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
            Text("Research runs on this Mac. Only search queries and page requests leave it.")
                .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func notice(_ title: String, detail: String) -> some View {
        VStack(spacing: 10) {
            Text(title).font(.system(size: 20, weight: .semibold))
            Text(detail).font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.center).frame(maxWidth: 520)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func chip(_ text: String, icon: String) -> some View {
        Label(text, systemImage: icon).font(.system(size: 10, weight: .medium))
            .foregroundStyle(Theme.textSecondary).padding(.horizontal, 8).padding(.vertical, 4)
            .background(Capsule().fill(Theme.chipFill)).overlay(Capsule().stroke(Theme.chipStroke))
    }

    private func labeled<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            content()
        }
    }

    private func labeledSmall<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 10)).foregroundStyle(Theme.textMuted)
            content()
        }
    }
}

/// The notch's text field: plain, on a chip fill, like the rest of its controls.
private struct NotchField: ViewModifier {
    func body(content: Content) -> some View {
        content
            .textFieldStyle(.plain)
            .font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 9).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(Theme.chipStroke, lineWidth: 1))
    }
}

/// Notch fields in the compact layout, the system rounded field in the window.
private struct AdaptiveField: ViewModifier {
    let compact: Bool
    func body(content: Content) -> some View {
        if compact { content.modifier(NotchField()) } else { content.textFieldStyle(.roundedBorder) }
    }
}
