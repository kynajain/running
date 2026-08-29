import SwiftUI

struct PermissionsView: View {
    @EnvironmentObject private var coordinator: SyncCoordinator

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "heart.text.square")
                .font(.system(size: 56))
                .foregroundStyle(.pink)
            Text("Connect Apple Health")
                .font(.title2.bold())
            VStack(alignment: .leading, spacing: 8) {
                ForEach(HealthMetric.allCases, id: \.self) { metric in
                    Label(metric.title, systemImage: "checkmark.circle")
                }
                Label("Workouts and routes", systemImage: "checkmark.circle")
            }
            .font(.callout)
            Text("Data is read on device and only leaves it if you configure a sync endpoint.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Allow Health access") {
                Task { await coordinator.requestAuthorization() }
            }
            .buttonStyle(.borderedProminent)
            .disabled(coordinator.state == .authorizing)
            if case .failed(let message) = coordinator.state {
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
        }
        .padding(32)
    }
}
