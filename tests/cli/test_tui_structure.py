"""Architecture guards for the decomposed Textual console."""

import pytest

pytest.importorskip("textual")

from ctrlrtn.cli.tui.control_actions import ControlActions  # noqa: E402
from ctrlrtn.cli.tui.control_actions.experiments import (  # noqa: E402
    LiveExperimentControlActions,
)
from ctrlrtn.cli.tui.control_actions.git_config import (  # noqa: E402
    GitConfigControlActions,
)
from ctrlrtn.cli.tui.control_actions.routes import (  # noqa: E402
    RouteControlActions,
)
from ctrlrtn.cli.tui.control_actions.selection import (  # noqa: E402
    ControlSelectionMixin,
)
from ctrlrtn.cli.tui.control_actions.shadows import (  # noqa: E402
    ShadowControlActions,
)
from ctrlrtn.cli.tui.forms import (  # noqa: E402
    ConfirmScreen,
    WorkflowIdentifyScreen,
)
from ctrlrtn.cli.tui.tables import ConsoleTables  # noqa: E402
from ctrlrtn.cli.tui.tables.detail import TableDetailMixin  # noqa: E402
from ctrlrtn.cli.tui.tables.fill import TableFillMixin  # noqa: E402
from ctrlrtn.cli.tui.tables.refresh import TableRefreshMixin  # noqa: E402


def test_table_facade_composes_refresh_fill_and_detail_capabilities():
    assert ConsoleTables.__bases__ == (
        TableRefreshMixin,
        TableFillMixin,
        TableDetailMixin,
    )
    assert not {
        name
        for name, value in ConsoleTables.__dict__.items()
        if callable(value) and not name.startswith("__")
    }


def test_control_actions_compose_independent_action_families():
    assert ControlActions.__bases__ == (
        ShadowControlActions,
        GitConfigControlActions,
        LiveExperimentControlActions,
        RouteControlActions,
        ControlSelectionMixin,
    )


def test_form_facade_exports_classes_owned_by_focused_modules():
    assert WorkflowIdentifyScreen.__module__.endswith(".forms.workflows")
    assert ConfirmScreen.__module__.endswith(".forms.common")
