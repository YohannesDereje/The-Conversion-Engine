"""
Convert interim_report.html -> interim_report.pdf

Tries weasyprint first (best quality), falls back to instructions if not installed.

Usage:
    python generate_pdf.py
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
HTML = ROOT / "interim_report.html"
PDF  = ROOT / "interim_report.pdf"


def try_weasyprint():
    try:
        from weasyprint import HTML as WH
        print("Using weasyprint...")
        WH(filename=str(HTML)).write_pdf(str(PDF))
        print(f"PDF written: {PDF}")
        return True
    except ImportError:
        return False
    except Exception as e:
        print(f"weasyprint error: {e}")
        return False


def try_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Chromium\Application\chrome.exe",
    ]
    for chrome in candidates:
        if pathlib.Path(chrome).exists():
            print(f"Using Chrome headless: {chrome}")
            result = subprocess.run([
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--print-to-pdf={PDF}",
                "--print-to-pdf-no-header",
                str(HTML),
            ], capture_output=True, text=True, timeout=60)
            if PDF.exists():
                print(f"PDF written: {PDF}")
                return True
            print(f"Chrome stderr: {result.stderr[:300]}")
    return False


def main():
    if not HTML.exists():
        print(f"Error: {HTML} not found")
        sys.exit(1)

    if try_weasyprint():
        return

    print("weasyprint not installed, trying Chrome headless...")
    if try_chrome():
        return

    print("\nAutomatic PDF generation unavailable. To create the PDF manually:")
    print(f"  1. Open in Chrome:  {HTML}")
    print("  2. Press Ctrl+P → destination 'Save as PDF'")
    print("     → Layout: Portrait, Margins: Default, Background graphics: ON")
    print("  3. Save as interim_report.pdf in the same folder")
    print("\nOr install weasyprint:")
    print("  pip install weasyprint")
    print("  python generate_pdf.py")


if __name__ == "__main__":
    main()
