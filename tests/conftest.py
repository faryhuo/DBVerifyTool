import os

import pytest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


@pytest.fixture
def sample_project():
    return os.path.join(FIXTURES, "sample_project")


@pytest.fixture
def sample_changelog(sample_project):
    return os.path.join(sample_project, "changelogs", "changelog_R1.xml")
