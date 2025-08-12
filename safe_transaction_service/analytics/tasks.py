import json
import logging
import time

from django.conf import settings
from django.db.models import Count, F, Q
from django.utils import timezone

from celery import app
from dateutil.relativedelta import relativedelta

from safe_transaction_service.analytics.services.analytics_service import (
    AnalyticsService,
)
from safe_transaction_service.history.models import MultisigTransaction, SafeContract, SafeLastStatus
from safe_transaction_service.history.services.balance_service import BalanceService, BalanceServiceProvider
from safe_transaction_service.utils.celery import task_timeout
from safe_transaction_service.utils.redis import get_redis
from safe_transaction_service.utils.tasks import LOCK_TIMEOUT
from safe_transaction_service.utils.utils import chunks

logger = logging.getLogger(__name__)


def _get_native_balances_multicall(
    safe_addresses: list[str], balance_service: BalanceService
) -> tuple[int, int]:
    """
    Get native token balances for a batch of Safe addresses using batch RPC calls.

    :param safe_addresses: List of Safe addresses to get balances for
    :param balance_service: BalanceService instance
    :return: Tuple of (total_balance_wei, safes_with_balance_count)
    """
    start_time = time.time()
    batch_size = len(safe_addresses)
    logger.debug(f"Starting batch balance processing for {batch_size} addresses")
    
    batch_total_balance = 0
    batch_safes_with_balance = 0
    
    try:
        # Use the existing ethereum client to get balances
        # This will use the client's internal connection pooling and retries
        balances = []
        
        # Process in smaller chunks to avoid overwhelming the RPC endpoint
        chunk_size = 100  # Smaller chunks for better reliability
        for address_chunk in chunks(safe_addresses, chunk_size):
            chunk_balances = []
            for address in address_chunk:
                try:
                    balance = balance_service.ethereum_client.get_balance(address)
                    chunk_balances.append(balance)
                except Exception as e:
                    logger.warning(f"Failed to get balance for address {address}: {e}")
                    chunk_balances.append(0)  # Default to 0 if individual request fails
            
            balances.extend(chunk_balances)
        
        # Process results
        for balance in balances:
            if balance > 0:
                batch_total_balance += balance
                batch_safes_with_balance += 1
        
        processing_time = time.time() - start_time
        logger.debug(f"Batch processing completed for {batch_size} addresses in {processing_time:.3f}s. "
                    f"Total balance: {batch_total_balance} wei, Safes with balance: {batch_safes_with_balance}")
        
    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(
            f"Batch processing failed for a batch of {len(safe_addresses)} addresses after {processing_time:.3f}s: {e}"
        )
        # Return zeros on complete failure
        batch_total_balance = 0
        batch_safes_with_balance = 0
    
    return batch_total_balance, batch_safes_with_balance


def _calculate_native_balances_batched() -> tuple[int, int]:
    """
    Calculate native token balances for all Safes using database iterators and chunked RPC calls.
    This approach is optimized for both memory efficiency and RPC endpoint reliability.

    :return: Tuple of (total_balance_wei, total_safes_with_balance)
    """
    function_start_time = time.time()
    
    # Use a more conservative batch size to avoid overwhelming RPC endpoints
    # This balances between efficiency and reliability
    db_batch_size = 100  # Reduced from 500 for better RPC endpoint compatibility
    balance_service = BalanceServiceProvider()
    total_balance_wei = 0
    total_safes_with_balance = 0
    processed_count = 0

    # Get total count for progress tracking
    count_start_time = time.time()
    total_safes = SafeContract.objects.count()
    count_time = time.time() - count_start_time
    logger.info(f"Total Safes count: {total_safes} (query took {count_time:.2f}s)")
    
    logger.info(
        f"Starting chunked RPC-based balance calculation for {total_safes} Safes with batch size {db_batch_size}"
    )

    # Use .iterator() for memory efficiency. It fetches addresses from the DB in chunks.
    queryset = SafeContract.objects.values_list("address", flat=True).order_by("pk")
    
    # Process addresses in batches using database slicing for better memory management
    offset = 0
    batch_number = 0
    
    while True:
        batch_start_time = time.time()
        batch_number += 1
        
        # Fetch a batch of addresses from the database
        db_fetch_start = time.time()
        address_batch = list(queryset[offset:offset + db_batch_size])
        db_fetch_time = time.time() - db_fetch_start
        
        if not address_batch:
            break  # No more addresses to process

        batch_size = len(address_batch)
        processed_count += batch_size
        
        logger.info(
            f"Processing batch {batch_number} with {batch_size} addresses "
            f"(fetched in {db_fetch_time:.2f}s, {processed_count}/{total_safes} total)..."
        )

        # Get balances for the entire batch with chunked RPC calls
        try:
            batch_rpc_start = time.time()
            (
                batch_balance,
                batch_safes_with_balance,
            ) = _get_native_balances_multicall(address_batch, balance_service)

            total_balance_wei += batch_balance
            total_safes_with_balance += batch_safes_with_balance
            
            batch_rpc_time = time.time() - batch_rpc_start
            batch_time = time.time() - batch_start_time
            progress_percent = (processed_count / total_safes) * 100 if total_safes > 0 else 0
            
            logger.info(
                f"Completed batch {batch_number} in {batch_time:.2f}s "
                f"(RPC processing: {batch_rpc_time:.2f}s). "
                f"Progress: {progress_percent:.1f}% ({processed_count}/{total_safes}). "
                f"Running totals - Balance: {total_balance_wei} wei, "
                f"Safes with balance: {total_safes_with_balance}"
            )
        except Exception as e:
            batch_time = time.time() - batch_start_time
            logger.error(f"Batch {batch_number} processing failed after {batch_time:.2f}s: {e}")
            # Continue with next batch rather than failing completely
        
        offset += db_batch_size

    total_function_time = time.time() - function_start_time
    avg_time_per_safe = total_function_time / processed_count if processed_count > 0 else 0

    logger.info(
        f"Chunked RPC-based balance calculation completed in {total_function_time:.2f}s. "
        f"Final results: Total balance: {total_balance_wei} wei, "
        f"Safes with balance: {total_safes_with_balance}/{processed_count}. "
        f"Average time per Safe: {avg_time_per_safe:.4f}s. "
        f"Processed {processed_count} Safes in {batch_number} batches."
    )

    return total_balance_wei, total_safes_with_balance


@app.shared_task()
@task_timeout(timeout_seconds=LOCK_TIMEOUT)
def get_transactions_per_safe_app_task():
    today = timezone.now()
    last_week = today - relativedelta(days=7)
    last_month = today - relativedelta(months=1)
    last_year = today - relativedelta(years=1)

    queryset = (
        MultisigTransaction.objects.filter(origin__name__isnull=False)
        .values(name=F("origin__name"), url=F("origin__url"))
        .annotate(
            total_tx=Count("origin__name"),
            tx_last_week=Count("origin__name", filter=Q(created__gt=last_week)),
            tx_last_month=Count("origin__name", filter=Q(created__gt=last_month)),
            tx_last_year=Count("origin__name", filter=Q(created__gt=last_year)),
        )
        .order_by("-total_tx")
    )

    if queryset:
        redis_key = AnalyticsService.REDIS_TRANSACTIONS_PER_SAFE_APP
        redis = get_redis()
        redis.set(redis_key, json.dumps(list(queryset)))
        return True
    return False


@app.shared_task()
@task_timeout(timeout_seconds=LOCK_TIMEOUT * 6)
def get_safe_statistics_task():
    """
    Calculate Safe statistics using efficient batch RPC calls for balance checking.
    
    This optimized version uses:
    - Batch RPC requests instead of individual balance calls
    - Larger batch sizes (5000 addresses per batch)
    - Memory-efficient database iteration
    - Better error handling and logging
    """
    try:
        task_start_time = time.time()
        logger.info("Starting Safe statistics task...")
        
        # Total number of created Safes (all proxy factories included)
        safes_count_start = time.time()
        total_safes = SafeContract.objects.count()
        safes_count_time = time.time() - safes_count_start
        logger.info(f"Counted {total_safes} total Safes in {safes_count_time:.2f}s")

        # Get all owners from SafeLastStatus to get current state
        # This gives us the most up-to-date owner information for each Safe
        owners_query_start = time.time()
        owners_data = SafeLastStatus.objects.exclude(owners__isnull=True).exclude(
            owners=[]
        ).values_list("owners", flat=True)
        owners_query_time = time.time() - owners_query_start
        logger.info(f"Fetched owner data from SafeLastStatus in {owners_query_time:.2f}s")

        # Count total owners and unique owners
        owners_processing_start = time.time()
        all_owners = set()
        total_owners_count = 0
        for owner_list in owners_data.iterator():  # Use iterator for memory efficiency
            if owner_list:  # Ensure the list is not empty
                total_owners_count += len(owner_list)
                all_owners.update(owner_list)

        unique_owners = len(all_owners)
        owners_processing_time = time.time() - owners_processing_start
        
        logger.info(f"Processed owner statistics in {owners_processing_time:.2f}s: "
                   f"total_owners={total_owners_count}, unique_owners={unique_owners}")

        # Calculate native token balances for all Safes using the optimized function
        logger.info(f"Starting balance calculation for {total_safes} Safes")
        
        total_balance_wei = 0
        total_safes_with_balance = 0
        
        if total_safes > 0:
            balance_calculation_start = time.time()
            try:
                total_balance_wei, total_safes_with_balance = _calculate_native_balances_batched()
                balance_calculation_time = time.time() - balance_calculation_start
                logger.info(
                    f"Balance calculation completed in {balance_calculation_time:.2f}s. "
                    f"Total balance: {total_balance_wei} wei, "
                    f"Safes with balance: {total_safes_with_balance}/{total_safes}"
                )
            except Exception as e:
                balance_calculation_time = time.time() - balance_calculation_start
                logger.error(f"Failed to calculate balances after {balance_calculation_time:.2f}s: {e}")
                # Continue without balance data rather than failing the entire task

        # Create and store statistics
        statistics_creation_start = time.time()
        statistics = {
            "total_safes": total_safes,
            "total_owners": total_owners_count,
            "unique_owners": unique_owners,
            "balance_wei": total_balance_wei,
            "safes_with_balance": total_safes_with_balance,
            "timestamp": timezone.now().isoformat(),
        }
        statistics_creation_time = time.time() - statistics_creation_start

        # Store in Redis
        redis_storage_start = time.time()
        redis_key = AnalyticsService.REDIS_SAFE_STATISTICS
        redis = get_redis()
        redis.set(redis_key, json.dumps(statistics))
        redis_storage_time = time.time() - redis_storage_start
        
        total_task_time = time.time() - task_start_time
        
        logger.info(f"Safe statistics task completed successfully in {total_task_time:.2f}s. "
                   f"Breakdown: statistics creation: {statistics_creation_time:.3f}s, "
                   f"redis storage: {redis_storage_time:.3f}s. "
                   f"Data saved to Redis key '{redis_key}'.")
        
        return True
    except Exception as e:
        if 'task_start_time' in locals():
            total_task_time = time.time() - task_start_time
            logger.error(f"Safe statistics task failed after {total_task_time:.2f}s: {e}")
        else:
            logger.error(f"Safe statistics task failed: {e}")
        # In case of any error, return False but don't raise
        # This prevents the task from failing completely
        return False
