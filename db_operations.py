import os
import pyodbc
import logging
import mysql.connector
from datetime import datetime, timedelta, date
from mysql.connector import Error as MySQLError

def close_connections(*connections):
    for conn in connections:
        if conn:
            conn.close()
    logging.info("Connections closed")

def clean_column_name(column_name):
    return column_name.strip().replace('#', '_')

def connect_odbc(dsn):
    """Establish a readonly connection to the ODBC source."""
    try:
        logging.info(f"Connecting to ODBC source using DSN: {dsn} (readonly)")
        return pyodbc.connect(f"DSN={dsn};READONLY=YES")
    except pyodbc.Error as e:
        logging.error(f"Error connecting to ODBC: {str(e)}", exc_info=True)
        raise

def connect_mysql(host, user, password, database):
    """
    Connect to the MySQL database.
    
    Parameters:
        host (str): The MySQL server host.
        user (str): The MySQL username.
        password (str): The MySQL password.
        database (str): The database name to connect to.

    Returns:
        mysql.connector.connection_cext.CMySQLConnection: A connection object to interact with MySQL.
    """
    try:
        connection = mysql.connector.connect(
            host=host,
            user=user,
            password=password,
            database=database
        )
        logging.info(f"Connected to MySQL database at {host}")
        return connection
    except mysql.connector.Error as err:
        logging.error(f"Error connecting to MySQL: {err}", exc_info=True)
        raise

def create_mysql_table_from_odbc_metadata(mysql_conn, destination_table, columns, primary_key, unique_keys, exceptions):
    """
    Create a MySQL table based on ODBC metadata and mapping exceptions.
    """
    type_mapping = {
        "TEXT": "TEXT",
        "STRING": "VARCHAR(255)",  # Map STRING to VARCHAR by default
        "DATE": "DATE",
        "TIME": "TIME",
        "INT": "INT",
        "FLOAT": "FLOAT",
        "DECIMAL": "DECIMAL(10,2)",
        "BOOLEAN": "TINYINT(1)"
    }

    logging.debug(f"ODBC metadata for table `{destination_table}`: {columns}")
    cursor = mysql_conn.cursor()

    column_definitions = []
    for col in columns:
        col_name = col[0]
        odbc_type = col[1]
        custom_type = None
        length = None

        # Apply exceptions if provided
        if exceptions and col_name in exceptions:
            exception = exceptions[col_name]
            custom_type = exception.get("type", "").upper()
            length = exception.get("length")

            # Handle custom types and validate
            if custom_type in type_mapping:
                col_type = type_mapping[custom_type]
                if custom_type.startswith("VARCHAR") and length:
                    col_type = f"VARCHAR({length})"
            elif custom_type == "VARCHAR":
                col_type = f"VARCHAR({length or 255})"  # Default to VARCHAR(255) if length isn't provided
            else:
                raise ValueError(f"Invalid MySQL type '{custom_type}' for column '{col_name}' in exceptions.")
        else:
            # Map ODBC type to MySQL type
            col_type = type_mapping.get(odbc_type.upper(), "TEXT")

        # Handle primary/unique key length for TEXT columns
        if col_name in primary_key or col_name in unique_keys:
            if col_type.startswith("TEXT"):
                key_length = exceptions.get(col_name, {}).get("key_length", 100)
                col_type = f"VARCHAR({key_length})"

        column_definitions.append(f"`{col_name}` {col_type}")
        
    # Add `created_at` and `updated_at` columns
    column_definitions.append("`created_at` DATETIME DEFAULT CURRENT_TIMESTAMP")
    column_definitions.append("`updated_at` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")


    # Add primary and unique keys
    if primary_key:
        primary_key_str = ", ".join([f"`{pk}`" for pk in primary_key])
        column_definitions.append(f"PRIMARY KEY ({primary_key_str})")

    if unique_keys:
        unique_key_str = ", ".join([f"`{uk}`" for uk in unique_keys])
        column_definitions.append(f"UNIQUE KEY ({unique_key_str})")

    # Create the table
    create_query = f"CREATE TABLE IF NOT EXISTS `{destination_table}` (\n  {', '.join(column_definitions)}\n)"
    logging.debug(f"Creating MySQL table `{destination_table}` with query: {create_query}")
    logging.info(f"Creating MySQL table `{destination_table}`")

    try:
        cursor.execute(create_query)
        mysql_conn.commit()
        logging.info(f"MySQL table `{destination_table}` created successfully.")
    except Exception as e:
        logging.error(f"Error creating MySQL table `{destination_table}`: {e}", exc_info=True)
    finally:
        cursor.close()

def does_table_exist(mysql_conn, table_name):
    """Check if a table exists in the MySQL database."""
    mysql_cursor = mysql_conn.cursor()
    try:
        mysql_cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
        result = mysql_cursor.fetchone()
        return result is not None
    except MySQLError as e:
        logging.error(f"Error checking existence of table {table_name}: {str(e)}", exc_info=True)
        return False

def drop_mysql_table_if_exists(mysql_conn, table_name):
    """
    Drop a MySQL table if it exists.
    
    Parameters:
        mysql_conn: MySQL connection object.
        table_name (str): The name of the table to drop.
    
    Returns:
        None
    """
    try:
        cursor = mysql_conn.cursor()
        query = f"DROP TABLE IF EXISTS `{table_name}`"
        cursor.execute(query)
        mysql_conn.commit()
        logging.info(f"Table `{table_name}` dropped successfully (if it existed).")
    except Exception as e:
        logging.error(f"Error dropping table `{table_name}`: {str(e)}", exc_info=True)
        raise
    finally:
        cursor.close()

def fetch_and_insert_rows(
    chunk_size,
    odbc_conn, mysql_conn, source_table, destination_table, columns, primary_key, unique_keys,
    sort_column, exceptions=None, since=None, trim_trailing_spaces=False, insert_columns=None
):
    """
    Fetch ODBC data starting from an offset and insert it into MySQL with `created_at` and `updated_at`.
    """
    cursor = odbc_conn.cursor()

    normalized_columns = [col[0].strip().upper() for col in columns]
    normalized_primary_keys = [pk.strip().upper() for pk in primary_key]

    if since and sort_column:
        look_back_date = (datetime.now() - timedelta(days=since)).strftime('%Y-%m-%d')
        date_filter = f"{sort_column} > '{look_back_date}'"
    else:
        date_filter = None

    where_clause = f"WHERE {date_filter}" if date_filter else ""
    query = f"""
        SELECT * FROM {source_table}
        {where_clause}
        ORDER BY {sort_column}
    """

    logging.debug(f"Executing query: {query}")
    batch_number = 1

    # Add `created_at` and `updated_at` columns for insertion only
    additional_columns = [("created_at", "DATETIME"), ("updated_at", "DATETIME")]
    insert_columns = columns + additional_columns

    try:
        cursor.execute(query)
        while True:
            try:
                chunk = cursor.fetchmany(chunk_size)
            except pyodbc.DataError as e:
                logging.error(f"DataError while fetching rows from {source_table}: {str(e)}")
                continue  # Skip invalid row

            if not chunk:
                logging.info(f"No more rows to process for table {source_table}.")
                break

            converted_chunk = []
            for row in chunk:
                try:
                    # Process the row based on original columns
                    processed_row = process_row(row, columns, exceptions, trim_trailing_spaces)

                    # Append `created_at` and `updated_at` timestamps for insertion
                    now = datetime.now()
                    processed_row = list(processed_row)
                    processed_row.append(now)  # created_at
                    processed_row.append(now)  # updated_at

                    converted_chunk.append(tuple(processed_row))
                except Exception as e:
                    logging.warning(f"Error processing row {row}: {str(e)}")
                    continue  # Skip invalid row

            logging.info(f"Inserting batch {batch_number} into `{destination_table}`.")
            insert_data_to_mysql(
                mysql_conn,
                destination_table,
                insert_columns,  # Use updated columns list with timestamps
                converted_chunk,
                primary_key,
                batch_size=chunk_size,
                exceptions=exceptions
            )
            batch_number += 1

    except pyodbc.Error as e:
        logging.error(f"Error fetching data from ODBC table {source_table}: {str(e)}", exc_info=True)

def fetch_and_update_rows(
    odbc_conn, mysql_conn, source_table, destination_table, columns, primary_key, unique_keys,
    sort_column, update_columns, chunk_size, exceptions=None, trim_trailing_spaces=False, since=None
):
    """
    Fetch rows from ODBC and update them in the MySQL table with error handling for bad records.
    """
    cursor = odbc_conn.cursor()

    # Add `created_at` and `updated_at` columns dynamically for updates
    additional_columns = [("created_at", "DATETIME"), ("updated_at", "DATETIME")]
    final_columns = columns + additional_columns

    # Prepare the base query
    base_query = f"SELECT * FROM {source_table}"

    # Apply `since` filter if provided
    date_filter = None
    if since and sort_column:
        look_back_date = (datetime.now() - timedelta(days=since)).strftime('%Y-%m-%d')
        date_filter = f"{sort_column} IS NOT NULL AND {sort_column} > '{look_back_date}'"
    else:
        date_filter = f"{sort_column} IS NOT NULL"  # Ignore rows with NULL sort_column

    # Finalize the query with filters and sorting
    where_clause = f"WHERE {date_filter}" if date_filter else ""
    query = f"{base_query} {where_clause} ORDER BY {sort_column}"
    logging.info(f"Executing query: {query}")

    # Prepare the update query for MySQL
    update_query = f"""
        INSERT INTO `{destination_table}` ({', '.join([f"`{col[0]}`" for col in final_columns])})
        VALUES ({', '.join(['%s'] * len(final_columns))})
        ON DUPLICATE KEY UPDATE
        {', '.join([f"`{col}`=VALUES(`{col}`)" for col in update_columns])},
        `updated_at`=VALUES(`updated_at`)
    """

    try:
        cursor.execute(query)
        while True:
            try:
                chunk = cursor.fetchmany(chunk_size)
                if not chunk:
                    logging.info(f"No more rows to process for table {source_table}.")
                    break
            except pyodbc.DataError as e:
                logging.error(f"DataError while fetching rows from {source_table}: {str(e)}")
                continue  # Skip the problematic batch

            converted_chunk = []
            bad_records = []  # Collect bad rows in this batch

            for row in chunk:
                try:
                    # Process the row based on the original ODBC columns
                    processed_row = process_row(row, columns, exceptions, trim_trailing_spaces)

                    # Append `created_at` and `updated_at` timestamps for insertion
                    now = datetime.now()
                    processed_row = list(processed_row)
                    processed_row.append(now)  # created_at
                    processed_row.append(now)  # updated_at

                    converted_chunk.append(tuple(processed_row))
                except Exception as e:
                    logging.warning(f"Error processing row {row}: {str(e)}")
                    bad_records.append(row)  # Log the bad record for debugging

            # Log bad records to a file
            if bad_records:
                bad_log_file = f"bad_records_{destination_table}.log"
                with open(bad_log_file, "a") as f:
                    for bad_row in bad_records:
                        f.write(f"{bad_row}\n")
                logging.warning(f"{len(bad_records)} bad rows logged to {bad_log_file}")

            # Execute the update query for valid rows
            try:
                mysql_cursor = mysql_conn.cursor()
                mysql_cursor.executemany(update_query, converted_chunk)
                mysql_conn.commit()
                logging.info(f"Batch of {len(converted_chunk)} rows updated in `{destination_table}`.")
            except Exception as e:
                logging.error(f"Error updating batch in table {destination_table}: {str(e)}", exc_info=True)

    except Exception as e:
        logging.error(f"Error fetching data from ODBC table {source_table}: {str(e)}", exc_info=True)

def fetch_odbc_metadata(odbc_conn, source_table, exceptions=None):
    """
    Fetch metadata (columns and types) for a table from the ODBC source.
    """
    try:
        cursor = odbc_conn.cursor()
        query = f"SELECT * FROM {source_table} WHERE 1=0"  # Fetch only metadata
        cursor.execute(query)
        metadata = cursor.description
        cursor.close()

        columns_metadata = []
        for column in metadata:
            column_name = column[0]
            column_type = column[1].__name__  # Assuming the type is a Python type
            if exceptions and column_name in exceptions:
                column_type = exceptions[column_name].get("type", column_type)
            columns_metadata.append((column_name, column_type))

        return columns_metadata
    except Exception as e:
        logging.error(f"Failed to fetch metadata for table {source_table}: {str(e)}")
        raise

def insert_data_to_mysql(mysql_conn,destination_table,columns,chunk,primary_key,batch_size,exceptions=None,trim_trailing_spaces=False):
    """
    Insert processed rows into a MySQL table.
    """
    cursor = mysql_conn.cursor()
    column_names = ', '.join([f"`{col[0]}`" for col in columns])
    placeholders = ', '.join(['%s'] * len(columns))
    insert_query = f"INSERT INTO `{destination_table}` ({column_names}) VALUES ({placeholders})"

    # Handle ON DUPLICATE KEY UPDATE for primary/unique keys
    if primary_key:
        update_columns = ', '.join([f"`{col}`=VALUES(`{col}`)" for col in primary_key])
        insert_query += f" ON DUPLICATE KEY UPDATE {update_columns}"

    logging.info(f"Preparing to insert {len(chunk)} rows into `{destination_table}`.")

    processed_chunk = []
    for row in chunk:
        try:
            processed_row = process_row(row, columns, exceptions, trim_trailing_spaces)
            processed_chunk.append(processed_row)
        except Exception as e:
            logging.error(f"Error processing row: {row} - {str(e)}")

    try:
        cursor.executemany(insert_query, processed_chunk)
        mysql_conn.commit()
        logging.info(f"Batch of {len(processed_chunk)} rows committed to `{destination_table}`.")
    except Exception as e:
        logging.error(f"Error inserting batch into `{destination_table}`: {str(e)}", exc_info=True)

def migrate_table_with_difference(chunk_size,
    mysql_conn, odbc_conn, source_table, destination_table, primary_key, unique_keys,
    update_columns, sort_column, exceptions, trim_trailing_spaces, insert_columns
):
    """
    Migrate a single table from ODBC to MySQL, handling differences in data.
    """

    try:
        # Fetch ODBC metadata
        logging.info(f"Fetching ODBC metadata for table: {source_table}")
        columns_metadata = fetch_odbc_metadata(odbc_conn, source_table, exceptions)
        # logging.info(f"Fetched metadata for table `{source_table}`: {columns_metadata}")
        
        # Create MySQL table if it doesn't exist
        create_mysql_table_from_odbc_metadata(
            mysql_conn,
            destination_table,
            columns_metadata,
            primary_key,
            unique_keys,
            exceptions
        )
        
        # Fetch and insert rows
        fetch_and_insert_rows(
            odbc_conn=odbc_conn,
            mysql_conn=mysql_conn,
            source_table=source_table,
            destination_table=destination_table,
            columns=columns_metadata,
            primary_key=primary_key,
            unique_keys=unique_keys,
            sort_column=sort_column,
            exceptions=exceptions,
            insert_columns=insert_columns,
            trim_trailing_spaces=trim_trailing_spaces,
            chunk_size=chunk_size
        )
    except Exception as e:
        logging.error(f"Failed to migrate table `{source_table}` to `{destination_table}`: {e}", exc_info=True)

def process_row(row, columns, exceptions, trim_trailing_spaces):
    """
    Process a single row by applying exceptions, validating and formatting dates/times, and trimming values.
    """
    processed_row = []
    for idx, col in enumerate(columns):
        col_name = col[0]
        value = row[idx]

        try:
            # Handle exceptions for specific column types
            if exceptions and col_name in exceptions:
                exception = exceptions[col_name]

                if exception.get("type") == "TIME":
                    time_format = exception.get("format", "%I:%M %p")
                    if value is None or (isinstance(value, str) and not value.strip()):
                        value = None
                    elif isinstance(value, str):
                        try:
                            # Normalize the input
                            value = value.strip().upper()
                            if not value.endswith("AM") and not value.endswith("PM"):
                                value = value.replace("P", "PM").replace("A", "AM")
                            
                            value = datetime.strptime(value, time_format).strftime('%H:%M:%S')
                        except ValueError:
                            # Handle valid 24-hour time format
                            try:
                                value = datetime.strptime(value.strip(), "%H:%M:%S").strftime('%H:%M:%S')
                            except ValueError:
                                logging.warning(f"Unrecognized TIME value for column {col_name}: {value}")
                                value = None
                    elif isinstance(value, datetime):
                        value = value.strftime('%H:%M:%S')
                    else:
                        logging.warning(f"Unexpected TIME value for column {col_name}: {value} (type: {type(value)})")
                        value = None

                # Updated DATE logic
                if exception.get("type") == "DATE":
                    try:
                        if value is None or (isinstance(value, str) and not value.strip()):
                            value = None
                        elif isinstance(value, str):
                            try:
                                # Try parsing as standard MySQL date
                                value = datetime.strptime(value.strip(), "%Y-%m-%d").date()
                            except ValueError:
                                # Fallback to parsing as mm/dd/yyyy
                                value = datetime.strptime(value.strip(), "%m/%d/%Y").date()
                        elif isinstance(value, (datetime, date)):
                            # Convert datetime or date to MySQL-compatible format
                            value = value.date()
                        else:
                            logging.warning(f"Unexpected DATE value for column {col_name}: {value} (type: {type(value)})")
                            value = None
                    except Exception as e:
                        logging.error(f"Unexpected error while processing DATE for column {col_name}: {value} - {str(e)}")
                        value = None


            # General trimming for text values
            if trim_trailing_spaces and isinstance(value, str):
                value = value.strip()

        except Exception as e:
            logging.error(f"Unexpected error while processing column {col_name}: {value} - {str(e)}")
            value = None

        processed_row.append(value)

    return tuple(processed_row)
