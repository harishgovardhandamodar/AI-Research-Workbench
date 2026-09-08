from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown

console = Console()

def print_markdown(md: str):
    console.print(Markdown(md))
