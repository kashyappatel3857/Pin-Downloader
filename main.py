import os
import sys
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any

# Configure Windows terminal for UTF-8 compatibility
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
    from rich import print as rprint
    HAS_RICH = True
    console = Console(highlight=False)
except ImportError:
    HAS_RICH = False
    console = None

from downloader import PinterestDownloader

DEFAULT_LINKS_FILE = "links.txt"
DEFAULT_OUTPUT_DIR = "downloads"

def print_banner():
    title = "[*] PINTEREST HIGH-QUALITY MEDIA DOWNLOADER [*]"
    subtitle = "Downloads Master Originals (PNG/JPG) & Max-Resolution HD Videos"
    if HAS_RICH:
        console.print(Panel(
            f"[bold cyan]{title}[/bold cyan]\n[dim white]{subtitle}[/dim white]",
            border_style="bright_blue",
            expand=False
        ))
    else:
        print("=" * 65)
        print(title)
        print(subtitle)
        print("=" * 65)

def read_links_from_file(filepath: str) -> List[str]:
    """Read Pinterest links from a file, ignoring comments (#) and blanks."""
    if not os.path.exists(filepath):
        return []
    links = []
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                # Extract URL if surrounded by quotes, spaces, or text
                for part in line.split():
                    clean_part = part.strip('",\'()[]<>')
                    if 'pinterest.com' in clean_part or 'pin.it' in clean_part:
                        links.append(clean_part)
                        break
                    elif clean_part.startswith('http://') or clean_part.startswith('https://'):
                        links.append(clean_part)
                        break

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for link in links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped

def process_single_pin(downloader: PinterestDownloader, link: str, progress=None, task_id=None) -> List[Dict[str, Any]]:
    """
    Extract and download media for a pin (including multiple items for carousels).
    Returns list of result dictionaries.
    """
    results = []
    display_name = link.split('?')[0].rsplit('/', 1)[-1] or link[:30]
    
    try:
        if progress and task_id:
            progress.update(task_id, description=f"[cyan]Extracting:[/] {display_name}...")

        media_items = downloader.extract_pin_media(link)
        total_items = len(media_items)

        for item in media_items:
            res = {
                'url': link,
                'pin_id': item.get('pin_id', 'N/A'),
                'title': item.get('title') or item.get('description') or 'Pin ' + item.get('pin_id', ''),
                'type': item.get('media_type', 'image').capitalize(),
                'quality': item.get('quality_info', 'Original'),
                'status': 'Failed',
                'message': '',
                'filepath': item.get('filepath', '')
            }

            filename = item.get('filename', 'media')
            item_num = f"[{item.get('item_index')}/{total_items}] " if total_items > 1 else ""

            def on_progress(downloaded, total):
                if progress and task_id and total > 0:
                    progress.update(
                        task_id,
                        completed=downloaded,
                        total=total,
                        description=f"[green]Downloading {item_num}:[/] {filename[:28]}"
                    )

            if progress and task_id:
                progress.update(task_id, description=f"[yellow]Downloading {item_num}:[/] {filename[:28]}")

            success, msg = downloader.download_media(item, progress_callback=on_progress)
            res['status'] = 'Success' if success else 'Failed'
            res['message'] = msg
            if "Already downloaded" in msg:
                res['status'] = 'Skipped (Exists)'

            results.append(res)

    except Exception as e:
        results.append({
            'url': link,
            'pin_id': 'Unknown',
            'title': 'Error',
            'type': 'Unknown',
            'quality': 'Unknown',
            'status': 'Failed',
            'message': str(e),
            'filepath': ''
        })

    if progress and task_id:
        if results:
            last = results[-1]
            status_color = "green" if "Success" in last['status'] else ("yellow" if "Skipped" in last['status'] else "red")
            progress.update(task_id, completed=100, total=100, description=f"[{status_color}]{last['status']}:[/] {last['pin_id']}")

    return results

def expand_links(downloader: PinterestDownloader, raw_links: List[str]) -> List[str]:
    """Expand board links into individual pin links."""
    expanded = []
    for link in raw_links:
        if downloader.is_board_url(link):
            if HAS_RICH:
                console.print(f"[cyan]Expanding Pinterest board:[/] {link} ...")
            else:
                print(f"Expanding Pinterest board: {link} ...")
            board_pins = downloader.extract_board_pins(link)
            if board_pins:
                if HAS_RICH:
                    console.print(f"  -> Found [bold green]{len(board_pins)}[/bold green] pins in board.")
                else:
                    print(f"  -> Found {len(board_pins)} pins in board.")
                expanded.extend(board_pins)
            else:
                if HAS_RICH:
                    console.print("  [yellow]-> No pins found or board is private.[/yellow]")
                else:
                    print("  -> No pins found or board is private.")
        else:
            expanded.append(link)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for lk in expanded:
        if lk not in seen:
            seen.add(lk)
            deduped.append(lk)
    return deduped

def download_batch(downloader: PinterestDownloader, raw_links: List[str], max_workers: int = 3):
    """Download a list of links with rich progress monitoring and summary report."""
    links = expand_links(downloader, raw_links)
    
    if not links:
        if HAS_RICH:
            console.print("[yellow]No valid links to download.[/yellow]")
        else:
            print("No valid links to download.")
        return

    if HAS_RICH:
        console.print(f"\n[bold green]Ready to download {len(links)} pin(s)...[/bold green]\n")
        
        all_results = []
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console
        ) as progress:
            
            if len(links) == 1:
                t_id = progress.add_task("[cyan]Processing...", total=100)
                item_results = process_single_pin(downloader, links[0], progress, t_id)
                all_results.extend(item_results)
            else:
                tasks = {}
                for link in links:
                    label = link.split('?')[0].rsplit('/', 1)[-1] or link[:30]
                    tasks[link] = progress.add_task(f"[cyan]Queued:[/] {label[:30]}...", total=100)
                    
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_link = {
                        executor.submit(process_single_pin, downloader, link, progress, tasks[link]): link
                        for link in links
                    }
                    for future in as_completed(future_to_link):
                        all_results.extend(future.result())

        # Display Summary Report Table
        table = Table(title="Download Summary Report", show_header=True, header_style="bold magenta")
        table.add_column("Pin ID", style="dim", width=20)
        table.add_column("Type", width=8)
        table.add_column("Quality", width=24)
        table.add_column("Status", width=18)
        table.add_column("File / Details")

        success_count = 0
        skipped_count = 0
        failed_count = 0

        for r in all_results:
            status_style = "green" if r['status'] == "Success" else ("yellow" if "Skipped" in r['status'] else "red")
            table.add_row(
                str(r['pin_id']),
                str(r['type']),
                str(r['quality']),
                f"[{status_style}]{r['status']}[/{status_style}]",
                os.path.basename(r['filepath']) if r['filepath'] else r['message']
            )
            if r['status'] == 'Success':
                success_count += 1
            elif 'Skipped' in r['status']:
                skipped_count += 1
            else:
                failed_count += 1

        console.print("\n")
        console.print(table)
        console.print(f"\n[bold green]Complete![/bold green] Downloaded: [bold]{success_count}[/bold] | Skipped: [bold]{skipped_count}[/bold] | Failed: [bold]{failed_count}[/bold]")
        console.print(f"[bold cyan]Downloads folder:[/bold cyan] [underline]{os.path.abspath(downloader.output_dir)}[/underline]\n")
    else:
        print(f"\nReady to download {len(links)} pin(s)...")
        all_results = []
        for i, link in enumerate(links, 1):
            print(f"[{i}/{len(links)}] Extracting: {link} ...")
            item_results = process_single_pin(downloader, link)
            for res in item_results:
                print(f"  -> {res['status']}: {res['message']}")
                all_results.append(res)
            
        print("\n" + "=" * 60)
        print("DOWNLOAD SUMMARY")
        print("=" * 60)
        for r in all_results:
            print(f"{r['pin_id']} | {r['type']} | {r['quality']} | {r['status']} | {os.path.basename(r['filepath'])}")
        print(f"\nSaved in: {os.path.abspath(downloader.output_dir)}\n")

def open_file_or_dir(path: str):
    """Open a file or directory using the OS default program (e.g., Notepad, File Explorer)."""
    if sys.platform == 'win32':
        os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.call(('open', path))
    else:
        subprocess.call(('xdg-open', path))

def interactive_menu(downloader: PinterestDownloader, links_file: str):
    """Interactive CLI menu when explicitly requested."""
    while True:
        print_banner()
        file_links = read_links_from_file(links_file)
        
        if HAS_RICH:
            console.print(f"[bold]Links file:[/bold] [underline]{links_file}[/underline] ([cyan]{len(file_links)} link(s) detected[/cyan])")
            console.print(f"[bold]Download folder:[/bold] [underline]{downloader.output_dir}[/underline]\n")
            console.print("[bold yellow]Options:[/bold yellow]")
            console.print("  [bold green]1.[/bold green] Download all links from links.txt")
            console.print("  [bold green]2.[/bold green] Enter / paste Pinterest link(s) manually")
            console.print("  [bold green]3.[/bold green] Open links.txt in Notepad (to edit/paste)")
            console.print("  [bold green]4.[/bold green] Open downloads folder in File Explorer")
            console.print("  [bold green]5.[/bold green] Exit")
        else:
            print(f"Links file: {links_file} ({len(file_links)} link(s) detected)")
            print(f"Download folder: {downloader.output_dir}\n")
            print("1. Download all links from links.txt")
            print("2. Enter / paste Pinterest link(s) manually")
            print("3. Open links.txt in Notepad (to edit/paste)")
            print("4. Open downloads folder in File Explorer")
            print("5. Exit")

        choice = input("\nEnter choice (1-5): ").strip()

        if choice == '1':
            if not file_links:
                if HAS_RICH:
                    console.print(f"\n[red]No links found in {links_file}![/red]")
                    console.print(f"Please paste links into [cyan]{links_file}[/cyan] first.\n")
                else:
                    print(f"\nNo links found in {links_file}! Please add links first.\n")
                input("Press Enter to continue...")
                continue

            download_batch(downloader, file_links)
            input("Press Enter to return to menu...")

        elif choice == '2':
            print("\nEnter Pinterest URL(s) (separate multiple with commas or spaces):")
            user_input = input("> ").strip()
            if user_input:
                manual_links = [item.strip() for item in user_input.replace(',', ' ').split() if item.strip()]
                if manual_links:
                    download_batch(downloader, manual_links)
                else:
                    print("No links detected.")
            input("Press Enter to return to menu...")

        elif choice == '3':
            if not os.path.exists(links_file):
                with open(links_file, 'w', encoding='utf-8') as f:
                    f.write("# Paste Pinterest links here (one per line)\n")
            print(f"Opening {links_file}...")
            open_file_or_dir(links_file)
            input("Press Enter once you have saved your links in the file...")

        elif choice == '4':
            os.makedirs(downloader.output_dir, exist_ok=True)
            print(f"Opening {downloader.output_dir}...")
            open_file_or_dir(downloader.output_dir)
            input("Press Enter to return to menu...")

        elif choice in ['5', 'q', 'exit']:
            print("Goodbye!")
            break
        else:
            print("Invalid choice, please select 1-5.")

def main():
    parser = argparse.ArgumentParser(
        description="Download Pinterest pins at the highest original quality (Master Images & HD Videos)."
    )
    parser.add_argument(
        'urls',
        nargs='*',
        help="One or more Pinterest pin URLs to download directly."
    )
    parser.add_argument(
        '-f', '--file',
        default=DEFAULT_LINKS_FILE,
        help=f"Path to text file containing links (default: {DEFAULT_LINKS_FILE})."
    )
    parser.add_argument(
        '-o', '--output',
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for downloaded media (default: {DEFAULT_OUTPUT_DIR})."
    )
    parser.add_argument(
        '-w', '--workers',
        type=int,
        default=3,
        help="Number of concurrent download threads for batch processing (default: 3)."
    )
    parser.add_argument(
        '--menu',
        action='store_true',
        help="Open interactive CLI menu instead of auto-downloading."
    )

    args = parser.parse_args()
    downloader = PinterestDownloader(output_dir=args.output)

    # Mode 1: Explicit URLs passed via command line
    if args.urls:
        print_banner()
        download_batch(downloader, args.urls, max_workers=args.workers)
        return

    # Mode 2: Explicit --menu flag requested
    if args.menu:
        interactive_menu(downloader, args.file)
        return

    # Mode 3 (DEFAULT ZERO-FRICTION): Automatically read and download all links from links.txt
    print_banner()
    links = read_links_from_file(args.file)
    if links:
        if HAS_RICH:
            console.print(f"[bold cyan]Reading links from:[/] [underline]{args.file}[/underline] ([green]{len(links)} links found[/green])")
        else:
            print(f"Reading links from: {args.file} ({len(links)} links found)")
        download_batch(downloader, links, max_workers=args.workers)
    else:
        if HAS_RICH:
            console.print(f"\n[yellow]No links found in [underline]{args.file}[/underline].[/yellow]")
            console.print("[dim]Paste your Pinterest links into links.txt (one per line) and run again.[/dim]\n")
        else:
            print(f"\nNo links found in {args.file}.")
            print("Paste your Pinterest links into links.txt (one per line) and run again.\n")
        
        # Ensure file exists so user can edit right away
        if not os.path.exists(args.file):
            with open(args.file, 'w', encoding='utf-8') as f:
                f.write("# Paste Pinterest links here (one per line)\n")
        
        # Ask if user wants to open links.txt now
        try:
            choice = input(f"Would you like to open {args.file} now? [Y/n]: ").strip().lower()
            if choice in ['', 'y', 'yes']:
                open_file_or_dir(args.file)
        except Exception:
            pass

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user. Exiting...")
