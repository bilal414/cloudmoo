from django.test.runner import DiscoverRunner


class CloudMooDiscoverRunner(DiscoverRunner):
    """Discover the top-level test package and reject false-green runs."""

    def build_suite(self, test_labels=None, **kwargs):
        labels = list(test_labels or ("tests",))
        suite = super().build_suite(test_labels=labels, **kwargs)
        if suite.countTestCases() == 0:
            raise RuntimeError(
                "CloudMoo test discovery found zero tests; check the test path and image contents"
            )
        return suite
