import logging
from pynonymizer.database import get_temp_db_name, get_provider
from pynonymizer.fake import FakeColumnGenerator
from pynonymizer.strategy.parser import StrategyParser
from pynonymizer.strategy.config import read_config
from pynonymizer.exceptions import ArgumentValidationError, DatabaseConnectionError
from pynonymizer.process_steps import StepActionMap, ProcessSteps


logger = logging.getLogger(__name__)


def pynonymize(
    input_path=None,
    strategyfile_path=None,
    output_path=None,
    db_user=None,
    db_password=None,
    db_type=None,
    db_host=None,
    db_name=None,
    db_port=None,
    fake_locale=None,
    only_step=None,
    start_at_step=None,
    stop_at_step=None,
    skip_steps=None,
    seed_rows=None,
    dry_run=False,
    verbose=False,
    ignore_anonymization_errors=False,
    **kwargs,
):
    """
    Runs a pynonymize process as if the CLI had been invoked.

    :raises:
        ArgumentValidationError: used when kwargs are missing or unable to be auto-resolved.

    """
    # Default and Normalize args
    if only_step is not None:
        only_step = ProcessSteps.from_value(only_step)

    if start_at_step is None:
        start_at_step = ProcessSteps.START
    else:
        start_at_step = ProcessSteps.from_value(start_at_step)

    if stop_at_step is None:
        stop_at_step = ProcessSteps.END
    else:
        stop_at_step = ProcessSteps.from_value(stop_at_step)

    if skip_steps and len(skip_steps) > 0:
        skip_steps = [ProcessSteps.from_value(skip) for skip in skip_steps]

    if db_type is None:
        db_type = "mysql"

    actions = StepActionMap(
        start_at_step=start_at_step,
        stop_at_step=stop_at_step,
        skip_steps=skip_steps,
        dry_run=dry_run,
        only_step=only_step,
    )

    # Validate mandatory args (depends on step actions)
    validations = []

    if not actions.skipped(ProcessSteps.RESTORE_DB):
        if input_path is None:
            validations.append("Missing INPUT")

    if not actions.skipped(ProcessSteps.ANONYMIZE_DB):
        if strategyfile_path is None:
            validations.append("Missing STRATEGYFILE")
        else:
            # only auto-determine the db_name if we have a strategyfile AND we are anonymizing.
            if db_name is None:
                db_name = get_temp_db_name(strategyfile_path)

    if not actions.skipped(ProcessSteps.DUMP_DB):
        if output_path is None:
            validations.append("Missing OUTPUT")

    # Mysql supports my.cnf files with additional config, so we have to assume db_host, db_user, db_password, db_port could all be in there
    if db_type != "mysql":
        if db_user is None:
            validations.append("Missing DB_USER")

        # postgres supports implicit db_pass using the .pgpass file
        if db_type != "postgres":
            if db_password is None:
                validations.append("Missing DB_PASSWORD")

    if db_name is None:
        validations.append("Missing DB_NAME: Auto-resolve failed.")

    if len(validations) > 0:
        raise ArgumentValidationError(validations)

    # init strategy as it relies on I/O - fail fast here preferred to after restore
    if not actions.skipped(ProcessSteps.ANONYMIZE_DB):
        strategy_parser = StrategyParser(fake_locale)

        logger.debug("loading strategyfile %s...", strategyfile_path)
        strategy = strategy_parser.parse_config(
            read_config(strategyfile_path), locale_override=fake_locale
        )

    # Discover db-type kwargs
    # mssql_backup_option -> backup_option and pass these to the constructor
    db_kwargs = {}
    db_arg_prefix = f"{db_type}_"
    for k, v in kwargs.items():
        if k.startswith(db_arg_prefix):
            db_kwargs[k[len(db_arg_prefix) :]] = v

    logger.debug(
        "Database: (%s:%s)%s@%s name: %s", db_host, db_port, db_type, db_user, db_name
    )
    db_provider = get_provider(
        type=db_type,
        db_host=db_host,
        db_user=db_user,
        db_pass=db_password,
        db_name=db_name,
        db_port=db_port,
        seed_rows=seed_rows,
        **db_kwargs,
    )

    # main process - no destructive/non-retryable actions should happen before this line ---
    logger.info(actions.summary(ProcessSteps.CREATE_DB))
    if not actions.skipped(ProcessSteps.CREATE_DB):
        db_provider.create_database()

    logger.info(actions.summary(ProcessSteps.RESTORE_DB))
    if not actions.skipped(ProcessSteps.RESTORE_DB):
        db_provider.restore_database(input_path)

    logger.info(actions.summary(ProcessSteps.ANONYMIZE_DB))
    if not actions.skipped(ProcessSteps.ANONYMIZE_DB):
        try:
            db_provider.anonymize_database(strategy)
        except Exception as e:
            if not ignore_anonymization_errors:
                raise e

    logger.info(actions.summary(ProcessSteps.DUMP_DB))
    if not actions.skipped(ProcessSteps.DUMP_DB):
        db_provider.dump_database(output_path)

    logger.info(actions.summary(ProcessSteps.DROP_DB))
    if not actions.skipped(ProcessSteps.DROP_DB):
        db_provider.drop_database()

    logger.info("Process complete!")
