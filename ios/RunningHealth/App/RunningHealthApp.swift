import SwiftUI

@main
struct RunningHealthApp: App {
    @StateObject private var settings: SyncSettings
    @StateObject private var coordinator: SyncCoordinator

    init() {
        let settings = SyncSettings()
        _settings = StateObject(wrappedValue: settings)
        _coordinator = StateObject(wrappedValue: SyncCoordinator(settings: settings))
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(settings)
                .environmentObject(coordinator)
                .task {
                    await coordinator.refreshAuthorization()
                    await coordinator.applyBackgroundDeliveryPreference()
                }
        }
    }
}
