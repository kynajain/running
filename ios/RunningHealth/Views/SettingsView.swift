import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: SyncSettings
    @EnvironmentObject private var coordinator: SyncCoordinator
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://sync.example.com/ingest", text: $settings.endpoint)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Bearer token (optional)", text: $settings.token)
                } header: {
                    Text("Sync endpoint")
                } footer: {
                    Text(settings.endpoint.isEmpty || settings.isUploadConfigured
                         ? "Records are POSTed as NDJSON. Leave empty to export a file instead."
                         : "Only https URLs are accepted.")
                    .foregroundStyle(settings.endpoint.isEmpty || settings.isUploadConfigured ? .secondary : .red)
                }

                Section {
                    Toggle("Sync in the background", isOn: $settings.backgroundDeliveryEnabled)
                } footer: {
                    Text("Uses HealthKit background delivery, which wakes the app at most once an hour.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onChange(of: settings.backgroundDeliveryEnabled) {
                Task { await coordinator.applyBackgroundDeliveryPreference() }
            }
        }
    }
}
