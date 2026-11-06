"""Select named matrix cases for controls; ordinary positive runs keep them all."""
import json
import os


def selected_cases(cases, variable):
    if variable not in os.environ:
        return cases
    names = json.loads(os.environ[variable])
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or name not in cases for name in names)):
        raise ValueError('invalid case selection: ' + variable)
    return {name: cases[name] for name in names}
