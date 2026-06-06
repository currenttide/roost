#if os(macOS)
import RoostKit
import SwiftUI
import UniformTypeIdentifiers

// File transfer UI (DESIGN.md §14): drag a file onto a worker — that's the
// whole feature. These are the confirm sheet, the fetch sheet, and the
// Transfers pane (history + the staged "fleet clipboard").

// MARK: - drop support

extension View {
    /// Makes any worker row a drop target for Finder files.
    func workerDropTarget(_ worker: Worker, model: AppModel) -> some View {
        modifier(WorkerDropTarget(worker: worker, model: model))
    }
}

private struct WorkerDropTarget: ViewModifier {
    let worker: Worker
    let model: AppModel
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: 4)
                    .fill(hovering ? Color.accentColor.opacity(0.15) : .clear))
            .dropDestination(for: URL.self) { urls, _ in
                let files = urls.filter { $0.isFileURL }
                guard !files.isEmpty else { return false }
                model.transfers.pendingSend = .init(worker: worker, files: files)
                return true
            } isTargeted: { hovering = $0 }
    }
}

// MARK: - send sheet

struct SendFileSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    let pending: TransferManager.PendingSend
    @State private var destination = ""

    private var file: URL? { pending.files.first }
    private var sizeLabel: String {
        guard let file,
              let bytes = try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize
        else { return "" }
        return ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if pending.files.count == 1, let file {
                Text("Send **\(file.lastPathComponent)** \(sizeLabel.isEmpty ? "" : "(\(sizeLabel)) ")to **\(pending.worker.name)**")
            } else {
                Text("Send **\(pending.files.count) files** to **\(pending.worker.name)**")
            }

            VStack(alignment: .leading, spacing: 4) {
                Text(pending.files.count == 1 ? "Destination" : "Destination folder")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("~/roost-inbox/…", text: $destination)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 320)
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Send") { send() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(destination.isEmpty)
            }
        }
        .padding(20)
        .onAppear {
            guard let file else { return }
            destination = pending.files.count == 1
                ? model.transfers.defaultDestination(
                    for: pending.worker, fileName: file.lastPathComponent)
                : (model.transfers.defaultDestination(
                    for: pending.worker, fileName: "x") as NSString)
                    .deletingLastPathComponent
        }
    }

    private func send() {
        if pending.files.count == 1, let file {
            model.transfers.send(file, to: pending.worker, destination: destination)
        } else {
            let dir = destination.hasSuffix("/") ? String(destination.dropLast()) : destination
            for file in pending.files {
                model.transfers.send(
                    file, to: pending.worker,
                    destination: "\(dir)/\(file.lastPathComponent)")
            }
        }
        model.openMainWindow?(nil)  // transfers progress lives in the window
        dismiss()
    }
}

// MARK: - fetch sheet

struct FetchFileSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    let worker: Worker
    @State private var remotePath = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Fetch a file from **\(worker.name)**")
            VStack(alignment: .leading, spacing: 4) {
                Text("Path on \(worker.name)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("~/results/output.log", text: $remotePath)
                    .textFieldStyle(.roundedBorder)
                    .frame(minWidth: 320)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Fetch…") { chooseDestinationAndGo() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(remotePath.isEmpty)
            }
        }
        .padding(20)
    }

    private func chooseDestinationAndGo() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = (remotePath as NSString).lastPathComponent
        panel.directoryURL = FileManager.default.urls(
            for: .downloadsDirectory, in: .userDomainMask).first
        guard panel.runModal() == .OK, let url = panel.url else { return }
        model.transfers.fetch(remotePath: remotePath, from: worker, saveTo: url)
        model.openMainWindow?(nil)
        dismiss()
    }
}

// MARK: - transfers pane

struct TransfersPane: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        List {
            transfersSection
            stagedSection
        }
        .task { try? await model.transfers.refreshStaged() }
    }

    @ViewBuilder
    private var transfersSection: some View {
        Section {
            let transfers = model.transfers.transfers
            if transfers.isEmpty {
                Text("Drag a file from Finder onto any worker to send it.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(transfers) { transfer in
                TransferRow(transfer: transfer)
            }
        } header: {
            HStack {
                Text("Transfers")
                Spacer()
                if model.transfers.transfers.contains(where: { $0.phase.isTerminal }) {
                    Button("Clear finished") { model.transfers.clearFinished() }
                        .buttonStyle(.link)
                        .font(.caption)
                }
            }
        }
    }

    @ViewBuilder
    private var stagedSection: some View {
        Section {
            ForEach(model.transfers.staged) { blob in
                HStack(spacing: 8) {
                    Image(systemName: "doc")
                        .foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(blob.name).font(.callout)
                        Text("\(ByteCountFormatter.string(fromByteCount: Int64(blob.size), countStyle: .file)) · expires \(Format.timeAgo(blob.expiresAt).replacingOccurrences(of: " ago", with: " from now"))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(blob.getURL, forType: .string)
                    } label: { Image(systemName: "link") }
                        .buttonStyle(.borderless)
                        .help("Copy fleet URL — paste it into a goal: “fetch <url> and run it”")
                    Button {
                        Task { await model.transfers.deleteStaged(blob) }
                    } label: { Image(systemName: "trash") }
                        .buttonStyle(.borderless)
                }
            }
            Button("Stage a file…") { stageViaPanel() }
                .buttonStyle(.link)
                .font(.caption)
        } header: {
            Text("Staged on the control plane")
        } footer: {
            Text("Staged files are addressable by agents (“fetch <url> and run it”) and expire automatically.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
    }

    private func stageViaPanel() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await model.transfers.stageFile(url) }
    }
}

private struct TransferRow: View {
    @Environment(AppModel.self) private var model
    let transfer: Transfer

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: transfer.direction == .send
                  ? "arrow.up.doc" : "arrow.down.doc")
                .foregroundStyle(color)
            VStack(alignment: .leading, spacing: 2) {
                Text(transfer.direction == .send
                     ? "\(transfer.fileName) → \(transfer.workerName)"
                     : "\(transfer.fileName) ← \(transfer.workerName)")
                    .font(.callout)
                HStack(spacing: 6) {
                    Text(transfer.phaseLabel)
                    if case .uploading(let fraction) = transfer.phase {
                        ProgressView(value: fraction)
                            .controlSize(.small)
                            .frame(width: 80)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            if case .done = transfer.phase, transfer.direction == .fetch,
               let local = transfer.localURL {
                Button("Reveal") {
                    NSWorkspace.shared.activateFileViewerSelecting([local])
                }
                .controlSize(.small)
            }
            if let jobID = transfer.jobID {
                Button("Run ↗") { model.openMainWindow?(jobID) }
                    .buttonStyle(.link)
                    .font(.caption)
            }
        }
        .padding(.vertical, 2)
    }

    private var color: Color {
        switch transfer.phase {
        case .failed: .red
        case .done: .green
        default: .blue
        }
    }
}
#endif
