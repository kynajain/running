import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var coordinator: SyncCoordinator

    var body: some View {
        Group {
            if !coordinator.isHealthDataAvailable {
                ContentUnavailableView(
                    "Health data unavailable",
                    systemImage: "heart.slash",
                    description: Text("This device does not provide HealthKit data.")
                )
            } else if coordinator.isAuthorized {
                DashboardView()
            } else {
                PermissionsView()
            }
        }
    }
}
