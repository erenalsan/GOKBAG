from login import LoginApp
from drone_controller import DroneController
import customtkinter

if __name__ == "__main__":
    customtkinter.set_appearance_mode("dark")
    root = customtkinter.CTk()
    app = LoginApp(root)
    root.mainloop()

    if app.logged_in:
        # same window for the controller: a second Tk root breaks CustomTkinter's timers
        root.unbind('<KeyRelease-Return>')
        root.unbind('<KeyRelease-KP_Enter>')
        for widget in root.winfo_children():
            widget.destroy()
        DroneController(root).run_app()
