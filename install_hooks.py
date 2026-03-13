import os
import site
import sys
import sysconfig


def get_scripts_dir() -> str | None:
    """Get the scripts directory where pip installs CLI tools."""
    if sys.platform != "win32":
        return None

    scripts_from_sysconfig = sysconfig.get_path("scripts")
    if scripts_from_sysconfig:
        return scripts_from_sysconfig

    scripts = os.path.join(site.getuserbase(), "Scripts")
    venv = os.path.join(sys.prefix, "Scripts")

    if os.path.exists(os.path.join(venv, "reposition.exe")):
        return venv
    return scripts


def add_to_path_windows(scripts_dir: str) -> bool:
    """Add scripts_dir to user PATH via registry."""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            current, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            current = ""

        current_parts = [p.strip().lower() for p in current.split(";") if p.strip()]
        if scripts_dir.strip().lower() not in current_parts:
            new_path = f"{current};{scripts_dir}" if current else scripts_dir
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
            winreg.CloseKey(key)
            return True

        winreg.CloseKey(key)
        return False
    except Exception:
        return False


if __name__ == "__main__":
    if sys.platform == "win32":
        scripts_dir = get_scripts_dir()
        if scripts_dir:
            added = add_to_path_windows(scripts_dir)
            print()
            if added:
                print("[OK] Added to PATH: " + scripts_dir)
                print("     Open a new terminal and run:")
                print("       reposition setup")
            else:
                print("[OK] reposition is ready. Run:")
                print("       reposition setup")