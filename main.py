import signal
import sys
from PySide6.QtCore import Qt, QTimer, QLoggingCategory
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox
from gui import STYLE, MainWindow
from login import LoginDialog

if __name__ == "__main__":
    # Known harmless Qt-on-Wayland notice when a window with a text field closes (the login); typing is unaffected
    QLoggingCategory.setFilterRules("qt.qpa.wayland.textinput=false")
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    # Ctrl+C in the terminal closes the window (which lands the drone) instead of being ignored
    signal.signal(signal.SIGINT, lambda *_: app.closeAllWindows())
    QTimer(app, timeout=lambda: None, interval=200).start()  # lets Python see Ctrl+C while Qt waits for events

    if LoginDialog().exec() != QDialog.Accepted:
        sys.exit(0)

    splash = QLabel("Connecting to the drone…")
    splash.setAlignment(Qt.AlignCenter)
    splash.setStyleSheet("font-size: 20px; padding: 40px 60px;")
    splash.setWindowTitle("GÖKBAĞ")
    splash.show()
    app.processEvents()
    try:
        from drone_controller import DroneController  # connects to the drone on import
        controller = DroneController()
    except Exception as e:
        splash.close()
        QMessageBox.critical(None, "Can't find the drone",
                             "Turn the drone on, connect this computer to the drone's Wi-Fi, then start again.\n\n"
                             f"Details: {e}")
        sys.exit(1)
    splash.close()

    window = MainWindow(controller)
    window.show()
    window.start()
    sys.exit(app.exec())
