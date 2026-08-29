import SwiftUI

struct DashboardView: View {
    @EnvironmentObject private var coordinator: SyncCoordinator
    @State private var showSettings = false
    @State private var exportFile: ExportFile?

    var body: some View {
        NavigationStack {
            List {
                Section("Status") {
                    LabeledContent("State", value: statusText)
                    LabeledContent("Pending records", value: "\(coordinator.pending.count)")
                    if let lastSyncedAt = coordinator.lastSyncedAt {
                        LabeledContent("Last sync", value: lastSyncedAt.formatted(.relative(presentation: .named)))
                    }
                }

                if !coordinator.recentWorkouts.isEmpty {
                    Section("Workouts") {
                        ForEach(coordinator.recentWorkouts.prefix(10), id: \.id) { workout in
                            WorkoutRow(workout: workout)
                        }
                    }
                }

                Section("Latest readings") {
                    ForEach(HealthMetric.allCases, id: \.self) { metric in
                        LabeledContent(metric.title, value: latestValue(for: metric))
                    }
                }
            }
            .navigationTitle("Running Health")
            .refreshable { await coordinator.sync() }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Export", systemImage: "square.and.arrow.up") {
                        exportFile = (try? coordinator.exportPending()).map(ExportFile.init)
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Settings", systemImage: "gear") { showSettings = true }
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(item: $exportFile) { file in ShareSheet(url: file.url) }
        }
    }

    private var statusText: String {
        switch coordinator.state {
        case .idle: "Idle"
        case .authorizing: "Requesting access"
        case .syncing: "Syncing"
        case .failed(let message): message
        }
    }

    private func latestValue(for metric: HealthMetric) -> String {
        guard let sample = coordinator.recentSamples.first(where: { $0.metric == metric.rawValue })
        else { return "—" }
        return "\(sample.value.formatted(.number.precision(.fractionLength(0...1)))) \(sample.unit)"
    }
}

private struct WorkoutRow: View {
    let workout: WorkoutSessionRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(workout.activity.replacingOccurrences(of: "HKWorkoutActivityType", with: ""))
                .font(.headline)
            Text(workout.start.formatted(date: .abbreviated, time: .shortened))
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("\((workout.distanceM / 1000).formatted(.number.precision(.fractionLength(2)))) km · \(workout.route.count) route points")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

private struct ExportFile: Identifiable {
    let url: URL
    var id: String { url.absoluteString }
}

private struct ShareSheet: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
