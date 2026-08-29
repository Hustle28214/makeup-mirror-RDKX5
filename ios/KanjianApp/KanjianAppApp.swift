import SwiftUI

@main
struct KanjianAppApp: App {
    @StateObject private var model = KanjianViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(red: 0.89, green: 0.88, blue: 0.83).ignoresSafeArea())
        }
    }
}
