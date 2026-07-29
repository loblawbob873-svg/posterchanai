"""
Database connection utilities for PostgreSQL.
Provides thread-safe connection management with auto-reconnection.
"""

import logging
import threading
import psycopg2

logger = logging.getLogger(__name__)


class DatabaseConnection:
    """
    Thread-safe PostgreSQL connection manager with auto-reconnection.

    Usage:
        db = DatabaseConnection(database='pleroma', user='user', password='pass')
        rows = db.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    """

    def __init__(self, database, user, password, host=None):
        """
        Initialize database connection.

        Args:
            database: Database name
            user: Database username
            password: Database password
            host: Database host (None for Unix socket)
        """
        self.database = database
        self.user = user
        self.password = password
        self.host = host
        self.conn = None
        self.lock = threading.Lock()

    def connect(self):
        """Establish database connection"""
        try:
            if self.host:
                self.conn = psycopg2.connect(
                    dbname=self.database,
                    user=self.user,
                    password=self.password,
                    host=self.host,
                    connect_timeout=10
                )
            else:
                self.conn = psycopg2.connect(
                    dbname=self.database,
                    user=self.user,
                    password=self.password,
                    connect_timeout=10
                )
            logger.debug(f"Database connection established to {self.database}")
            return True
        except Exception as e:
            # Log error without potentially exposing credentials
            error_type = type(e).__name__
            logger.error(f"Failed to connect to database {self.database}: {error_type}")
            logger.debug(f"Database connection error details: {e}")
            self.conn = None
            return False

    def execute(self, query, params=None):
        """
        Execute a SQL query with automatic reconnection on failure.

        Args:
            query: SQL query string
            params: Optional query parameters

        Returns:
            List of rows for SELECT queries, empty list otherwise
        """
        with self.lock:
            if self.conn is None or self.conn.closed:
                if not self.connect():
                    return []

            try:
                return self._execute_query(query, params)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Database connection lost: {e}. Attempting to reconnect...")
                self.conn = None
                if self.connect():
                    try:
                        return self._execute_query(query, params)
                    except Exception as retry_error:
                        logger.error(f"Query failed after reconnect: {retry_error}")
                        return []
                return []
            except Exception as e:
                logger.error(f"SQL query failed: {e}")
                return []

    def _execute_query(self, query, params):
        """Internal method to execute query"""
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            try:
                return cur.fetchall()
            except psycopg2.ProgrammingError:
                return []

    def close(self):
        """Close the database connection"""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


# Global database connection instance (initialized lazily)
_db_instance = None
_db_lock = threading.Lock()


def get_database(database=None, user=None, password=None, host=None):
    """
    Get or create a global database connection instance.

    Args are read from config if not provided.
    """
    global _db_instance

    with _db_lock:
        if _db_instance is None:
            from config import SQL_DATABASE, SQL_USER, SQL_PASS, SQL_HOST
            _db_instance = DatabaseConnection(
                database=database or SQL_DATABASE,
                user=user or SQL_USER,
                password=password or SQL_PASS,
                host=host or SQL_HOST
            )
            _db_instance.connect()
        return _db_instance


def run_psql(query, params=None):
    """
    Convenience function to run a SQL query.
    Uses the global database connection.
    """
    db = get_database()
    return db.execute(query, params)
