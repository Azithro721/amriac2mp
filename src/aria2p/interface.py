"""
This module contains all the code responsible for the HTOP-like interface.
"""

# Why using asciimatics?
#
# - curses is hard, and not working well on Windows
# - blessings (curses-based) is easier, but does not provide input methods and is not maintained
# - blessed (blessings fork) provides input methods, but they are blocking
# - urwid seems less easy to use than asciimatics, and older
# - clint is not maintained and does not provide input methods
# - prompt_toolkit is designed to build interactive (like, very interactive) command line applications
# - curtsies, pygcurse, unicurses, npyscreen: all based on curses anyway, which does not work well on Windows
#
# Well, asciimatics also provides a "top" example, so...

# pylint: disable=invalid-name

import os
import time
from collections import defaultdict

from asciimatics.event import KeyboardEvent, MouseEvent
from asciimatics.screen import ManagedScreen, Screen
from loguru import logger

from .api import API
from .utils import get_version


class Key:
    def __init__(self, name, value=None):
        self.name = name
        if value is None:
            value = ord(name)
        self.value = value

    def __eq__(self, value):
        return self.value == value


class Keys:
    """The actions and their shortcuts keys."""

    HELP = [Key("F1", Screen.KEY_F1), Key("h")]
    SETUP = [Key("F2", Screen.KEY_F2)]
    TOGGLE_RESUME_PAUSE = [Key("space", ord(" "))]
    PRIORITY_UP = [Key("F7", Screen.KEY_F7), Key("u"), Key("[")]
    PRIORITY_DOWN = [Key("F8", Screen.KEY_F8), Key("d"), Key("]")]
    REVERSE_SORT = [Key("I")]
    NEXT_SORT = [Key("n"), Key(">")]
    PREVIOUS_SORT = [Key("p"), Key("<")]
    SELECT_SORT = [Key("F6", Screen.KEY_F6)]
    REMOVE_ASK = [Key("F9", Screen.KEY_F9), Key("del", Screen.KEY_DELETE)]
    TOGGLE_EXPAND_COLLAPSE = [Key("x")]
    TOGGLE_EXPAND_COLLAPSE_ALL = [Key("X")]
    AUTOCLEAR = [Key("c")]
    FOLLOW_ROW = [Key("F")]
    SEARCH = [Key("F3", Screen.KEY_F3), Key("/")]
    FILTER = [Key("F4", Screen.KEY_F4), Key("\\")]
    TOGGLE_SELECT = [Key("s")]
    UN_SELECT_ALL = [Key("U")]
    QUIT = [Key("F10", Screen.KEY_F10), Key("q")]
    CANCEL = [Key("esc", Screen.KEY_ESCAPE)]
    ENTER = [Key("enter", ord("\n"))]
    MOVE_UP = [Key("up", Screen.KEY_UP)]
    MOVE_DOWN = [Key("down", Screen.KEY_DOWN)]
    MOVE_LEFT = [Key("left", Screen.KEY_LEFT)]
    MOVE_RIGHT = [Key("right", Screen.KEY_RIGHT)]

    @staticmethod
    def names(keys_list):
        return [key.name for key in keys_list]

    @staticmethod
    def values(keys_list):
        return [key.value for key in keys_list]


class Exit(Exception):
    """A simple exception to exit the interactive interface."""


class Column:
    """
    A class to specify a column in the interface.

    It's composed of a header (the string to display on top), a padding (how to align the text),
    and three callable functions to get the text from a Python object, to sort between these objects,
    and to get a color palette based on the text.
    """

    def __init__(self, header, padding, get_text, get_sort, get_palette):
        """
        Initialization method.

        Args:
            header (str): The string to display on top.
            padding (str): How to align the text.
            get_text (func): Function accepting a Download as argument and returning the text to display.
            get_sort (func): Function accepting a Download as argument and returning the attribute used to sort.
            get_palette (func): Function accepting text as argument and returning a palette or a palette identifier.
        """
        self.header = header
        self.padding = padding
        self.get_text = get_text
        self.get_sort = get_sort
        self.get_palette = get_palette


class HorizontalScroll:
    """
    A wrapper around asciimatics' Screen.print_at and Screen.paint methods.

    It allows scroll the rows horizontally, used when moving left and right:
    the first N characters will not be printed.
    """

    def __init__(self, screen, scroll=0):
        """
        Initialization method.

        Args:
            screen (Screen): The asciimatics screen object.
            scroll (int): Base scroll to use when printing. Will decrease by one with each character skipped.
        """
        self.screen = screen
        self.scroll = scroll

    def set_scroll(self, scroll):
        """Set the scroll value."""
        self.scroll = scroll

    def print_at(self, text, x, y, palette):
        """
        Wrapper print_at method.

        Args:
            text (str): Text to print.
            x (int): X axis position / column.
            y (int): Y axis position / row.
            palette (list/tuple): A length-3 tuple or a list of length-3 tuples representing asciimatics palettes.

        Returns:
            int: The number of characters actually printed.
        """
        logger.debug(f"Printing following text with offset={self.scroll}")
        logger.debug(text)
        if self.scroll == 0:
            if isinstance(palette, list):
                self.screen.paint(text, x, y, colour_map=palette)
            else:
                self.screen.print_at(text, x, y, *palette)
            written = len(text)
        else:
            text_length = len(text)
            if text_length > self.scroll:
                new_text = text[self.scroll :]
                written = len(new_text)
                if isinstance(palette, list):
                    new_palette = palette[self.scroll :]
                    self.screen.paint(new_text, x, y, colour_map=new_palette)
                else:
                    self.screen.print_at(new_text, x, y, *palette)
                self.scroll = 0
            elif text_length == self.scroll:
                self.scroll = 0
                written = 0
            else:
                self.scroll -= text_length
                written = 0
        return written


class Palette:
    """A simple class to hold palettes getters."""

    @staticmethod
    def status(value):
        """Return the palette for a STATUS cell."""
        return "status_" + value

    @staticmethod
    def name(value):
        """Return the palette for a NAME cell."""
        if value.startswith("[METADATA]"):
            return (
                [(Screen.COLOUR_GREEN, Screen.A_UNDERLINE, Screen.COLOUR_BLACK)] * 10
                + [Interface.palettes["metadata"]] * (len(value.strip()) - 10)
                + [Interface.palettes["row"]]
            )
        return "name"


class Interface:
    """
    The main class responsible of drawing the HTOP-like interface.

    It should be instantiated with an API instance, and then ran with its ``run`` method.

    If you want to re-use this class' code to create an HTOP-like interface for another purpose,
    simply change these few things:

    - columns, columns_order and palettes attributes
    - sort and reverse attributes default values
    - get_data method. It should return a list of objects that can be compared by equality (==, __eq__, __hash__)
    - __init__ method to accept other arguments
    - remove/change the few events with "download" or "self.api" in the process_event method
    """

    class State:
        MAIN = 0
        HELP = 1
        SETUP = 2
        REMOVE_ASK = 3
        SELECT_SORT = 4

    state = State.MAIN
    sleep = 0.005
    frames = 200  # 200 * 0.005 seconds == 1 second
    frame = 0
    focused = 0
    side_focused = 0
    sort = 2
    reverse = True
    x_scroll = 0
    x_offset = 0
    y_offset = 0
    row_offset = 0
    refresh = False
    width = None
    height = None
    screen = None
    data = []
    rows = []
    scroller = None
    follow = None
    bounds = []

    palettes = defaultdict(lambda: (Screen.COLOUR_WHITE, Screen.A_NORMAL, Screen.COLOUR_BLACK))
    palettes.update(
        {
            "header": (Screen.COLOUR_BLACK, Screen.A_NORMAL, Screen.COLOUR_GREEN),
            "focused_header": (Screen.COLOUR_BLACK, Screen.A_NORMAL, Screen.COLOUR_CYAN),
            "focused_row": (Screen.COLOUR_BLACK, Screen.A_NORMAL, Screen.COLOUR_CYAN),
            "status_active": (Screen.COLOUR_CYAN, Screen.A_NORMAL, Screen.COLOUR_BLACK),
            "status_paused": (Screen.COLOUR_YELLOW, Screen.A_NORMAL, Screen.COLOUR_BLACK),
            "status_waiting": (Screen.COLOUR_WHITE, Screen.A_BOLD, Screen.COLOUR_BLACK),
            "status_error": (Screen.COLOUR_RED, Screen.A_BOLD, Screen.COLOUR_BLACK),
            "status_complete": (Screen.COLOUR_GREEN, Screen.A_NORMAL, Screen.COLOUR_BLACK),
            "metadata": (Screen.COLOUR_WHITE, Screen.A_UNDERLINE, Screen.COLOUR_BLACK),
            "side_column_header": (Screen.COLOUR_BLACK, Screen.A_NORMAL, Screen.COLOUR_GREEN),
            "side_column_row": (Screen.COLOUR_WHITE, Screen.A_NORMAL, Screen.COLOUR_BLACK),
            "side_column_focused_row": (Screen.COLOUR_BLACK, Screen.A_NORMAL, Screen.COLOUR_CYAN),
            "bright_help": (Screen.COLOUR_CYAN, Screen.A_BOLD, Screen.COLOUR_BLACK),
        }
    )

    columns_order = ["gid", "status", "progress", "size", "down_speed", "up_speed", "eta", "name"]
    columns = {
        "gid": Column(
            header="GID", padding=">16", get_text=lambda d: d.gid, get_sort=lambda d: d.gid, get_palette=lambda d: "gid"
        ),
        "status": Column(
            header="STATUS",
            padding="<9",
            get_text=lambda d: d.status,
            get_sort=lambda d: d.status,
            get_palette=Palette.status,
        ),
        "progress": Column(
            header="PROGRESS",
            padding=">8",
            get_text=lambda d: d.progress_string(),
            get_sort=lambda d: d.progress,
            get_palette=lambda s: "progress",
        ),
        "size": Column(
            header="SIZE",
            padding=">11",
            get_text=lambda d: d.total_length_string(),
            get_sort=lambda d: d.total_length,
            get_palette=lambda s: "size",
        ),
        "down_speed": Column(
            header="DOWN_SPEED",
            padding=">13",
            get_text=lambda d: d.download_speed_string(),
            get_sort=lambda d: d.download_speed,
            get_palette=lambda s: "down_speed",
        ),
        "up_speed": Column(
            header="UP_SPEED",
            padding=">13",
            get_text=lambda d: d.upload_speed_string(),
            get_sort=lambda d: d.upload_speed,
            get_palette=lambda s: "up_speed",
        ),
        "eta": Column(
            header="ETA",
            padding=">8",
            get_text=lambda d: d.eta_string(precision=2),
            get_sort=lambda d: d.eta,
            get_palette=lambda s: "eta",
        ),
        "name": Column(
            header="NAME",
            padding="100%",
            get_text=lambda d: d.name,
            get_sort=lambda d: d.name,
            get_palette=Palette.name,
        ),
    }

    remove_ask_header = "Remove:"
    remove_ask_rows = [
        ("Remove", lambda d: d.remove(force=False, files=False)),
        ("Remove with files", lambda d: d.remove(force=False, files=True)),
        ("Force remove", lambda d: d.remove(force=True, files=False)),
        ("Force remove with files", lambda d: d.remove(force=True, files=True)),
    ]
    last_remove_choice = None

    select_sort_header = "Select sort:"
    select_sort_rows = columns_order

    def __init__(self, api=None):
        """
        Initialization method.

        Args:
            api (API): An instance of API.
        """
        if api is None:
            api = API()
        self.api = api

        # reduce curses' 1 second delay when hitting escape to 25 ms
        os.environ.setdefault("ESCDELAY", "25")

        self.state_mapping = {
            self.State.MAIN: {
                "process_keyboard_event": self.process_keyboard_event_main,
                "process_mouse_event": self.process_mouse_event_main,
                "print_functions": [self.print_table],
            },
            self.State.HELP: {
                "process_keyboard_event": self.process_keyboard_event_help,
                "process_mouse_event": self.process_mouse_event_help,
                "print_functions": [self.print_help],
            },
            self.State.SETUP: {
                "process_keyboard_event": self.process_keyboard_event_setup,
                "process_mouse_event": self.process_mouse_event_setup,
                "print_functions": [],
            },
            self.State.REMOVE_ASK: {
                "process_keyboard_event": self.process_keyboard_event_remove_ask,
                "process_mouse_event": self.process_mouse_event_remove_ask,
                "print_functions": [self.print_remove_ask_column, self.print_table],
            },
            self.State.SELECT_SORT: {
                "process_keyboard_event": self.process_keyboard_event_select_sort,
                "process_mouse_event": self.process_mouse_event_select_sort,
                "print_functions": [self.print_select_sort_column, self.print_table],
            },
        }

    def run(self):
        """The main drawing loop."""
        try:
            # outer loop to support screen resize
            while True:
                with ManagedScreen() as screen:
                    self.set_screen(screen)
                    self.frame = 0
                    # break (and re-enter) when screen has been resized
                    while not screen.has_resized():
                        # keep previous sort in memory to know if we have to re-sort the rows
                        # once all events are processed (to avoid useless/redundant sort passes)
                        previous_sort = (self.sort, self.reverse)

                        # we only refresh when explicitly asked for
                        self.refresh = False

                        # process all events before refreshing screen,
                        # otherwise the reactivity is slowed down a lot with fast inputs
                        event = screen.get_event()
                        while event:
                            # avoid crashing the interface if exceptions occur while processing an event
                            try:
                                self.process_event(event)
                            except Exit:
                                return True
                            except Exception as error:
                                # TODO: display error in status bar
                                logger.exception(error)
                            event = screen.get_event()

                        # time to update data and rows
                        if self.frame == 0:
                            self.update_data()
                            self.update_rows()
                            self.refresh = True

                        # time to refresh the screen
                        if self.refresh:
                            # sort if needed, unless it was just done at frame 0 when updating
                            if (self.sort, self.reverse) != previous_sort and self.frame != 0:
                                self.sort_data()
                                self.update_rows()

                            # actual printing and screen refresh
                            for print_function in self.state_mapping[self.state]["print_functions"]:
                                print_function()
                            screen.refresh()

                        # sleep and increment frame
                        time.sleep(self.sleep)
                        self.frame = (self.frame + 1) % self.frames
        except Exception as error:
            logger.exception(error)
            return False

    def update_select_sort_rows(self):
        self.select_sort_rows = self.columns_order

    def process_event(self, event):
        """
        Process an event.

        For reactivity purpose, this method should not compute expensive stuff, only change the state of the interface,
        changes that will be applied by update_data and update_rows methods.

        Args:
            event (KeyboardEvent/MouseEvent): The event to process.
        """
        if isinstance(event, KeyboardEvent):
            self.process_keyboard_event(event)

        elif isinstance(event, MouseEvent):
            self.process_mouse_event(event)

    def process_keyboard_event(self, event):
        self.state_mapping[self.state]["process_keyboard_event"](event)

    def process_keyboard_event_main(self, event):
        if event.key_code in Keys.MOVE_UP:
            if self.focused > 0:
                self.focused -= 1
                if self.focused < self.row_offset:
                    self.row_offset = self.focused
                elif self.focused >= self.row_offset + (self.height - 1):
                    # happens when shrinking height
                    self.row_offset = self.focused + 1 - (self.height - 1)
                self.follow = None
                self.refresh = True

        elif event.key_code in Keys.MOVE_DOWN:
            if self.focused < len(self.rows) - 1:
                self.focused += 1
                if self.focused - self.row_offset >= (self.height - 1):
                    self.row_offset = self.focused + 1 - (self.height - 1)
                self.follow = None
                self.refresh = True

        elif event.key_code in Keys.MOVE_LEFT:
            if self.x_scroll > 0:
                self.x_scroll = max(0, self.x_scroll - 5)
                self.refresh = True

        elif event.key_code in Keys.MOVE_RIGHT:
            self.x_scroll += 5
            self.refresh = True

        elif event.key_code in Keys.HELP:
            self.state = self.State.HELP
            self.refresh = True

        elif event.key_code in Keys.SETUP:
            pass  # TODO

        elif event.key_code in Keys.TOGGLE_RESUME_PAUSE:
            download = self.data[self.focused]
            if download.is_active or download.is_waiting:
                download.pause()
            elif download.is_paused:
                download.resume()

        elif event.key_code in Keys.PRIORITY_UP:
            download = self.data[self.focused]
            if not download.is_active:
                download.move_up()
                self.follow = download

        elif event.key_code in Keys.PRIORITY_DOWN:
            download = self.data[self.focused]
            if not download.is_active:
                download.move_down()
                self.follow = download

        elif event.key_code in Keys.REVERSE_SORT:
            self.reverse = not self.reverse
            self.refresh = True

        elif event.key_code in Keys.NEXT_SORT:
            if self.sort < len(self.columns) - 1:
                self.sort += 1
                self.refresh = True

        elif event.key_code in Keys.PREVIOUS_SORT:
            if self.sort > 0:
                self.sort -= 1
                self.refresh = True

        elif event.key_code in Keys.SELECT_SORT:
            self.state = self.State.SELECT_SORT
            self.side_focused = self.sort
            self.x_offset = self.width_select_sort() + 1
            self.refresh = True

        elif event.key_code in Keys.REMOVE_ASK:
            if self.follow_focused():
                self.state = self.State.REMOVE_ASK
                self.x_offset = self.width_remove_ask() + 1
                if self.last_remove_choice is not None:
                    self.side_focused = self.last_remove_choice
                self.refresh = True

        elif event.key_code in Keys.TOGGLE_EXPAND_COLLAPSE:
            pass  # TODO

        elif event.key_code in Keys.TOGGLE_EXPAND_COLLAPSE_ALL:
            pass  # TODO

        elif event.key_code in Keys.AUTOCLEAR:
            self.api.autopurge()

        elif event.key_code in Keys.FOLLOW_ROW:
            self.follow_focused()

        elif event.key_code in Keys.SEARCH:
            pass  # TODO

        elif event.key_code in Keys.FILTER:
            pass  # TODO

        elif event.key_code in Keys.TOGGLE_SELECT:
            pass  # TODO

        elif event.key_code in Keys.UN_SELECT_ALL:
            pass  # TODO

        elif event.key_code in Keys.QUIT:
            raise Exit()

    def process_keyboard_event_help(self, event):
        self.state = self.State.MAIN
        self.refresh = True

    def process_keyboard_event_setup(self, event):
        pass

    def process_keyboard_event_remove_ask(self, event):
        if event.key_code in Keys.CANCEL:
            self.state = self.State.MAIN
            self.x_offset = 0
            self.refresh = True

        elif event.key_code in Keys.ENTER:
            if self.follow:
                self.remove_ask_rows[self.side_focused][1](self.follow)
                self.follow = None
            self.last_remove_choice = self.side_focused
            self.state = self.State.MAIN
            self.x_offset = 0

            # force complete refresh
            self.frame = 0

        elif event.key_code in Keys.MOVE_UP:
            if self.side_focused > 0:
                self.side_focused -= 1
                self.refresh = True

        elif event.key_code in Keys.MOVE_DOWN:
            if self.side_focused < len(self.remove_ask_rows) - 1:
                self.side_focused += 1
                self.refresh = True

    def process_keyboard_event_select_sort(self, event):
        if event.key_code in Keys.CANCEL:
            self.state = self.State.MAIN
            self.x_offset = 0
            self.refresh = True

        elif event.key_code in Keys.ENTER:
            self.sort = self.side_focused
            self.state = self.State.MAIN
            self.x_offset = 0
            self.refresh = True

        elif event.key_code in Keys.MOVE_UP:
            if self.side_focused > 0:
                self.side_focused -= 1
                self.refresh = True

        elif event.key_code in Keys.MOVE_DOWN:
            if self.side_focused < len(self.select_sort_rows) - 1:
                self.side_focused += 1
                self.refresh = True

    def process_mouse_event(self, event):
        self.state_mapping[self.state]["process_mouse_event"](event)

    def process_mouse_event_main(self, event):
        if event.buttons & MouseEvent.LEFT_CLICK:
            if event.y == 0:
                new_sort = self.get_column_at_x(event.x)
                if new_sort == self.sort:
                    self.reverse = not self.reverse
                else:
                    self.sort = new_sort
            else:
                self.focused = min(event.y - 1 + self.row_offset, len(self.rows) - 1)
            self.refresh = True

        # elif event.buttons & MouseEvent.RIGHT_CLICK:
        #     pass  # TODO: expand/collapse

    def process_mouse_event_help(self, event):
        pass

    def process_mouse_event_setup(self, event):
        pass

    def process_mouse_event_remove_ask(self, event):
        pass

    def process_mouse_event_select_sort(self, event):
        pass

    def width_remove_ask(self):
        return max(len(self.remove_ask_header), max(len(row[0]) for row in self.remove_ask_rows))

    def width_select_sort(self):
        return max(len(column_name) for column_name in self.columns_order + [self.select_sort_header])

    def follow_focused(self):
        if self.focused < len(self.data):
            self.follow = self.data[self.focused]
            return True
        return False

    def print_help(self):
        version = get_version()
        lines = [f"aria2p {version} — (C) 2018-2019 Timothée Mazzucotelli", "Released under the ISC license.", ""]

        y = 0
        for line in lines:
            self.screen.print_at(f"{line:<{self.width}}", 0, y, *self.palettes["bright_help"])
            y += 1

        self.print_keys_text("Arrows:", " scroll downloads list", y)
        y += 1

        for keys, text in [
            (Keys.HELP, " show this help screen"),
            # (Keys.SETUP, " setup"),  # not implemented
            (Keys.TOGGLE_RESUME_PAUSE, " toggle pause/resume"),
            (Keys.PRIORITY_UP, " priority up (-)"),
            (Keys.PRIORITY_DOWN, " priority down (+)"),
            (Keys.REVERSE_SORT, " invert sort order"),
            (Keys.NEXT_SORT, " sort next column"),
            (Keys.PREVIOUS_SORT, " sort previous column"),
            (Keys.SELECT_SORT, " select sort column"),
            (Keys.REMOVE_ASK, " remove download"),
            # (Keys.TOGGLE_EXPAND_COLLAPSE, " toggle expand/collapse"),  # not implemented
            # (Keys.TOGGLE_EXPAND_COLLAPSE_ALL, " toggle expand/collapse all"),  # not implemented
            (Keys.AUTOCLEAR, " autopurge downloads"),
            (Keys.FOLLOW_ROW, " cursor follows download"),
            # (Keys.SEARCH, " name search"),  # not implemented
            # (Keys.FILTER, " name filtering"),  # not implemented
            # (Keys.TOGGLE_SELECT, " toggle select download"),  # not implemented
            # (Keys.UN_SELECT_ALL, " unselect all downloads"),  # not implemented
            (Keys.QUIT, " quit"),
        ]:
            self.print_keys(keys, text, y)
            y += 1

        self.screen.print_at(" " * self.width, 0, y)
        y += 1
        self.screen.print_at(f"{'Press any key to return.':<{self.width}}", 0, y, *self.palettes["bright_help"])
        y += 1

        for i in range(self.height - y):
            self.screen.print_at(" " * self.width, 0, y + i)

    def print_keys(self, keys, text, y):
        self.print_keys_text(" ".join(Keys.names(keys)) + ":", text, y)

    def print_keys_text(self, keys_text, text, y):
        length = 8
        padding = self.width - length
        self.screen.print_at(f"{keys_text:>{length}}", 0, y, *self.palettes["bright_help"])
        self.screen.print_at(f"{text:<{padding}}", length, y, *self.palettes["default"])

    def print_remove_ask_column(self):
        y = self.y_offset
        padding = self.width_remove_ask()
        header_string = f"{self.remove_ask_header:<{padding}}"
        len_header = len(header_string)
        self.screen.print_at(header_string, 0, y, *self.palettes["side_column_header"])
        self.screen.print_at(" ", len_header, y, *self.palettes["default"])
        for i, row in enumerate(self.remove_ask_rows):
            y += 1
            palette = (
                self.palettes["side_column_focused_row"] if i == self.side_focused else self.palettes["side_column_row"]
            )
            row_string = f"{row[0]:<{padding}}"
            len_row = len(row_string)
            self.screen.print_at(row_string, 0, y, *palette)
            self.screen.print_at(" ", len_row, y, *self.palettes["default"])

        for i in range(1, self.height - y):
            self.screen.print_at(" " * (padding + 1), 0, y + i)

    def print_select_sort_column(self):
        y = self.y_offset
        padding = self.width_select_sort()
        header_string = f"{self.select_sort_header:<{padding}}"
        len_header = len(header_string)
        self.screen.print_at(header_string, 0, y, *self.palettes["side_column_header"])
        self.screen.print_at(" ", len_header, y, *self.palettes["default"])
        for i, row in enumerate(self.select_sort_rows):
            y += 1
            palette = (
                self.palettes["side_column_focused_row"] if i == self.side_focused else self.palettes["side_column_row"]
            )
            row_string = f"{row:<{padding}}"
            len_row = len(row_string)
            self.screen.print_at(row_string, 0, y, *palette)
            self.screen.print_at(" ", len_row, y, *self.palettes["default"])

        for i in range(1, self.height - y):
            self.screen.print_at(" " * (padding + 1), 0, y + i)

    def print_table(self):
        self.print_headers()
        self.print_rows()

    def print_headers(self):
        """Print the headers (columns names)."""
        self.scroller.set_scroll(self.x_scroll)
        x, y, c = self.x_offset, self.y_offset, 0

        for column_name in self.columns_order:
            column = self.columns[column_name]
            palette = self.palettes["focused_header"] if c == self.sort else self.palettes["header"]

            if column.padding == "100%":
                header_string = f"{column.header}"
                fill_up = " " * max(0, self.width - x - len(header_string))
                written = self.scroller.print_at(header_string, x, y, palette)
                self.scroller.print_at(fill_up, x + written, y, self.palettes["header"])

            else:
                header_string = f"{column.header:{column.padding}} "
                written = self.scroller.print_at(header_string, x, y, palette)

            x += written
            c += 1

    def print_rows(self):
        """Print the rows."""
        y = self.y_offset + 1
        for row in self.rows[self.row_offset : self.row_offset + self.height]:

            self.scroller.set_scroll(self.x_scroll)
            x = self.x_offset

            for i, column_name in enumerate(self.columns_order):
                column = self.columns[column_name]
                padding = f"<{max(0, self.width - x)}" if column.padding == "100%" else column.padding

                if self.focused == y - self.y_offset - 1 + self.row_offset:
                    palette = self.palettes["focused_row"]
                else:
                    palette = column.get_palette(row[i])
                    if isinstance(palette, str):
                        palette = self.palettes[palette]

                field_string = f"{row[i]:{padding}} "
                written = self.scroller.print_at(field_string, x, y, palette)
                x += written

            y += 1

        for i in range(self.height - y):
            self.screen.print_at(" " * self.width, self.x_offset, y + i)

    def get_column_at_x(self, x):
        """For an horizontal position X, return the column index."""
        for i, bound in enumerate(self.bounds):
            if bound[0] <= x <= bound[1]:
                return i
        raise ValueError

    def set_screen(self, screen):
        """Set the screen object, its scroller wrapper, width, height, and columns bounds."""
        self.screen = screen
        self.height, self.width = screen.dimensions
        self.scroller = HorizontalScroll(screen)
        self.bounds = []
        for column_name in self.columns_order:
            column = self.columns[column_name]
            if column.padding == "100%":
                self.bounds.append((self.bounds[-1][1] + 1, self.width))
            else:
                padding = int(column.padding.lstrip("<>=^"))
                if not self.bounds:
                    self.bounds = [(0, padding)]
                else:
                    self.bounds.append((self.bounds[-1][1] + 1, self.bounds[-1][1] + 1 + padding))

    def get_data(self):
        """Return a list of objects."""
        return self.api.get_downloads()

    def update_data(self):
        """Set the interface data and rows contents."""
        self.data = self.get_data()
        self.sort_data()

    def sort_data(self):
        """Sort data according to interface state."""
        sort_function = self.columns[self.columns_order[self.sort]].get_sort
        self.data = sorted(self.data, key=sort_function, reverse=self.reverse)

    def update_rows(self):
        """Update rows contents according to data and interface state."""
        text_getters = [self.columns[c].get_text for c in self.columns_order]
        n_columns = len(self.columns_order)
        self.rows = [tuple(text_getters[i](item) for i in range(n_columns)) for item in self.data]
        if self.follow:
            self.focused = self.data.index(self.follow)