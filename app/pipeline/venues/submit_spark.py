"""Submit Track 2 venues-catalog job through reviewed fleet Spark runtime."""
from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

from pipeline.weather.submit_spark import connector_arguments, runtime_arguments

ROOT = Path(__file__).resolve().parent


def build_package(output=None) -> Path:
    """Package only Python files under pipeline/venues and shared pipeline modules."""
    target = Path(output) if output is not None else ROOT / 'runtime' / 'venues-code.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT.parent / '__init__.py', 'pipeline/__init__.py')
        archive.write(ROOT.parent / 'kafka_config.py', 'pipeline/kafka_config.py')
        archive.write(ROOT.parent / 'environment.py', 'pipeline/environment.py')
        for path in sorted(ROOT.glob('*.py')):
            archive.write(path, 'pipeline/venues/' + path.relative_to(ROOT).as_posix())
    return target


def main():
    if not os.environ.get('SPARK_MASTER'):
        raise RuntimeError('SPARK_MASTER_missing')
    arguments = connector_arguments() + runtime_arguments()
    archive = build_package(os.environ.get('VENUE_SPARK_PACKAGE_PATH'))
    command = [
        os.environ.get('SPARK_SUBMIT') or 'spark-submit',
        '--master',
        os.environ['SPARK_MASTER'],
        '--deploy-mode',
        'client',
        *arguments,
        '--py-files',
        str(archive),
    ]
    if os.environ.get('SPARK_DRIVER_HOST'):
        command.extend(['--conf', 'spark.driver.host=' + os.environ['SPARK_DRIVER_HOST']])
    command.append(str(ROOT / 'spark_job.py'))
    raise SystemExit(subprocess.call(command))


if __name__ == '__main__':
    main()
