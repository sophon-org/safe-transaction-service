"""
Django management command to manually insert Safe accounts into the database.

This command is designed for Safes that exist in the genesis block or were created
without proper proxy factory events that can be indexed automatically.
"""

from typing import List, Optional
from dataclasses import dataclass
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from eth_typing import ChecksumAddress, HexStr
from hexbytes import HexBytes

from safe_eth.eth import get_auto_ethereum_client
from safe_eth.safe import Safe
from safe_eth.safe.safe import SafeInfo
from safe_eth.safe.exceptions import CannotRetrieveSafeInfoException
from safe_eth.eth.utils import fast_to_checksum_address
from safe_eth.eth.contracts import get_safe_contract, get_proxy_factory_V1_4_1_contract

from ...models import (
    EthereumTx,
    EthereumBlock, 
    InternalTx,
    InternalTxType,
    SafeContract,
    SafeLastStatus,
    SafeMasterCopy,
)


@dataclass
class SafeData:
    """
    Data structure for Safe account information needed for manual insertion.
    Enhanced to support SafeCreationView API compatibility.
    
    Required fields:
    - address: Safe contract address
    - owners: List of owner addresses 
    - threshold: Number of required signatures
    - master_copy: Safe master copy/implementation address
    
    Optional fields:
    - nonce: Current nonce (default: 0)
    - fallback_handler: Fallback handler address (default: 0x0)
    - guard: Guard address (default: None)
    - enabled_modules: List of enabled module addresses (default: empty)
    - creation_block: Block number when Safe was created (default: 0)
    - creation_tx_hash: Transaction hash of creation (default: generated)
    
    SafeCreationView API fields:
    - factory_address: Address of the proxy factory that created the Safe
    - creator: Address that initiated the Safe creation
    - salt_nonce: Salt nonce used in CREATE2 deployment (if applicable)
    """
    address: ChecksumAddress
    owners: List[ChecksumAddress]
    threshold: int
    master_copy: ChecksumAddress
    nonce: int = 0
    fallback_handler: ChecksumAddress = "0x0000000000000000000000000000000000000000"
    guard: Optional[ChecksumAddress] = None
    enabled_modules: List[ChecksumAddress] = None
    creation_block: int = 0
    creation_tx_hash: Optional[HexStr] = None
    
    # SafeCreationView compatibility fields
    factory_address: ChecksumAddress = "0x0000000000000000000000000000000000000000"
    creator: ChecksumAddress = "0x0000000000000000000000000000000000000000"
    salt_nonce: Optional[int] = None

    def __post_init__(self):
        if self.enabled_modules is None:
            self.enabled_modules = []
        
        # Validate addresses
        self.address = fast_to_checksum_address(self.address)
        self.owners = [fast_to_checksum_address(owner) for owner in self.owners]
        self.master_copy = fast_to_checksum_address(self.master_copy)
        self.fallback_handler = fast_to_checksum_address(self.fallback_handler)
        self.factory_address = fast_to_checksum_address(self.factory_address)
        self.creator = fast_to_checksum_address(self.creator)
        
        if self.guard:
            self.guard = fast_to_checksum_address(self.guard)
        
        if self.enabled_modules:
            self.enabled_modules = [fast_to_checksum_address(module) for module in self.enabled_modules]
        
        # Generate a deterministic tx hash if not provided
        if not self.creation_tx_hash:
            # Create a deterministic hash based on Safe address and block
            import hashlib
            hash_input = f"{self.address}{self.creation_block}".encode()
            self.creation_tx_hash = HexStr("0x" + hashlib.sha256(hash_input).hexdigest())
        
        # Validation
        if len(self.owners) == 0:
            raise ValueError("At least one owner is required")
        if self.threshold <= 0 or self.threshold > len(self.owners):
            raise ValueError(f"Invalid threshold {self.threshold} for {len(self.owners)} owners")

    def generate_setup_data(self) -> bytes:
        """
        Generate Safe setup() function call data for SafeCreationView compatibility.
        
        This creates the initializer data that would normally be used during Safe creation.
        """
        try:
            ethereum_client = get_auto_ethereum_client()
            safe_contract = get_safe_contract(ethereum_client.w3, address=None)
            
            # Create setup function call
            # function setup(
            #     address[] calldata _owners,
            #     uint256 _threshold,
            #     address to,
            #     bytes calldata data,
            #     address fallbackHandler,
            #     address paymentToken,
            #     uint256 payment,
            #     address paymentReceiver
            # )
            setup_data = safe_contract.functions.setup(
                self.owners,  # _owners
                self.threshold,  # _threshold 
                "0x0000000000000000000000000000000000000000",  # to (no module setup call)
                b"",  # data (no module setup data)
                self.fallback_handler,  # fallbackHandler
                "0x0000000000000000000000000000000000000000",  # paymentToken (no payment)
                0,  # payment (no payment)
                "0x0000000000000000000000000000000000000000"  # paymentReceiver (no payment)
            ).build_transaction({'gas': 0})['data']
            
            return HexBytes(setup_data)
        except Exception as e:
            # If setup data generation fails, return empty bytes
            print(f"Warning: Could not generate setup data: {e}")
            return HexBytes("0x")

    def generate_proxy_factory_call_data(self) -> bytes:
        """
        Generate proxy factory function call data (createProxyWithNonce or createProxy).
        
        This creates the transaction data that would normally be sent to the proxy factory.
        Based on SafeService._decode_proxy_factory and account_abstraction.helpers.decode_init_code.
        """
        if self.factory_address == "0x0000000000000000000000000000000000000000":
            return HexBytes("0x")
            
        try:
            ethereum_client = get_auto_ethereum_client()
            proxy_factory = get_proxy_factory_V1_4_1_contract(ethereum_client.w3)
            
            # Generate the setup data that would be passed as initializer
            setup_data = self.generate_setup_data()
            
            # Choose between createProxy and createProxyWithNonce based on salt_nonce
            if self.salt_nonce is not None:
                # function createProxyWithNonce(address _singleton, bytes initializer, uint256 saltNonce)
                factory_call_data = proxy_factory.functions.createProxyWithNonce(
                    self.master_copy,  # _singleton (Safe master copy)
                    setup_data,        # initializer (setup call data)  
                    self.salt_nonce    # saltNonce
                ).build_transaction({'gas': 0})['data']
            else:
                # function createProxy(address _singleton, bytes initializer)  
                factory_call_data = proxy_factory.functions.createProxy(
                    self.master_copy,  # _singleton (Safe master copy)
                    setup_data         # initializer (setup call data)
                ).build_transaction({'gas': 0})['data']
            
            return HexBytes(factory_call_data)
        except Exception as e:
            # If factory call data generation fails, return placeholder  
            print(f"Warning: Could not generate proxy factory call data: {e}")
            return HexBytes("0x1234")  # Placeholder data

    @classmethod
    def from_onchain_state(
        cls, 
        address: ChecksumAddress, 
        creation_block: int = 0, 
        creation_tx_hash: Optional[HexStr] = None
    ) -> "SafeData":
        """
        Create SafeData by reading the current onchain state of a Safe.
        
        :param address: Safe contract address
        :param creation_block: Block number when Safe was created (default: 0)
        :param creation_tx_hash: Transaction hash of creation (default: auto-generated)
        :return: SafeData instance with onchain state
        :raises: ValueError if Safe cannot be read or doesn't exist
        """
        try:
            ethereum_client = get_auto_ethereum_client()
            safe = Safe(address, ethereum_client)
            safe_info: SafeInfo = safe.retrieve_all_info()
            
            return cls(
                address=safe_info.address,
                owners=safe_info.owners,
                threshold=safe_info.threshold,
                master_copy=safe_info.master_copy,
                nonce=safe_info.nonce,
                fallback_handler=safe_info.fallback_handler,
                guard=safe_info.guard if safe_info.guard != "0x0000000000000000000000000000000000000000" else None,
                enabled_modules=safe_info.modules,
                creation_block=creation_block,
                creation_tx_hash=creation_tx_hash,
            )
        except CannotRetrieveSafeInfoException as e:
            raise ValueError(f"Cannot retrieve Safe info for {address}: {e}")
        except Exception as e:
            raise ValueError(f"Error reading Safe state for {address}: {e}")


class Command(BaseCommand):
    help = """
    Manually insert Safe accounts into the database for genesis or non-indexed Safes.
    Enhanced to support SafeCreationView API compatibility.

    This command creates all required database records:
    - EthereumBlock (genesis/creation block)
    - EthereumTx (creation transaction)  
    - InternalTx (CREATE transaction for Safe deployment + setup call for SafeCreationView)
    - SafeContract (Safe contract record)
    - SafeLastStatus (current Safe state)
    
    SafeCreationView Compatibility:
    The command now creates proper InternalTx records with setup data so that the
    SafeCreationView API returns complete creation information instead of null values.
    
    New SafeData fields for SafeCreationView:
    - factory_address: Address of the proxy factory that created the Safe
    - creator: Address that initiated the Safe creation
    - salt_nonce: Salt nonce used in CREATE2 deployment (optional)
    
    Usage examples:
    
    1. Read from blockchain and insert:
       python manage.py manual_insert_safe_account_to_db --from-onchain 0x1234... 0x5678...
    
    2. Specify creation block for onchain Safes:
       python manage.py manual_insert_safe_account_to_db --from-onchain 0x1234... --creation-block 12345678
    
    3. Decode Safe state only (no insertion):
       python manage.py manual_insert_safe_account_to_db --from-onchain 0xAddress --decode-only
    
    4. Use manual configuration (edit SAFES_TO_INSERT in code):
       python manage.py manual_insert_safe_account_to_db
    
    5. Dry run to test:
       python manage.py manual_insert_safe_account_to_db --from-onchain 0xAddress --dry-run
    """

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Perform a dry run without actually inserting data",
        )
        parser.add_argument(
            "--from-onchain",
            nargs="+",
            help="Safe addresses to read state from blockchain and insert. Format: address:block_number (block_number optional)",
        )
        parser.add_argument(
            "--creation-block",
            type=int,
            default=0,
            help="Default creation block number for onchain Safes (default: 0)",
        )
        parser.add_argument(
            "--decode-only",
            action="store_true",
            help="Only decode and display Safe state from blockchain, don't insert into database",
        )

    def handle(self, *args, **options):
        """
        Main command handler. 
        
        You can either:
        1. Define your Safe accounts in the SAFES_TO_INSERT list below (for manual configuration)
        2. Use --from-onchain flag to automatically read Safe state from blockchain
        """
        dry_run = options["dry_run"]
        from_onchain = options.get("from_onchain")
        default_creation_block = options["creation_block"]
        decode_only = options["decode_only"]
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No data will be inserted")
            )

        # Handle onchain Safe reading
        if from_onchain:
            self.stdout.write(
                self.style.SUCCESS(f"Reading {len(from_onchain)} Safes from blockchain...")
            )
            safes_to_process = []
            
            for safe_spec in from_onchain:
                # Parse address:block_number or just address
                if ":" in safe_spec:
                    address, block_str = safe_spec.split(":", 1)
                    creation_block = int(block_str)
                else:
                    address = safe_spec
                    creation_block = default_creation_block
                
                try:
                    self.stdout.write(f"Reading onchain state for {address}...")
                    safe_data = SafeData.from_onchain_state(
                        address=address,
                        creation_block=creation_block
                    )
                    safes_to_process.append(safe_data)
                    self.stdout.write(
                        self.style.SUCCESS(f"✓ Successfully decoded Safe {address}")
                    )
                    # Display detailed information
                    self._display_safe_info(safe_data)
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f"✗ Failed to read {address}: {e}")
                    )
                    continue
            
            if decode_only:
                self.stdout.write(
                    self.style.SUCCESS(f"\n=== Decoded {len(safes_to_process)} Safes ===")
                )
                return
            
            # Process the onchain Safes
            self._process_safes(safes_to_process, dry_run)
            return

        # =================================================================
        # CONFIGURE YOUR SAFES HERE
        # =================================================================
        # Replace with your actual Safe data
        SAFES_TO_INSERT: List[SafeData] = [
            # Example Safe data - replace with your actual data
            # SafeData(
            #     address="0x1234567890123456789012345678901234567890",
            #     owners=[
            #         "0xOwner1Address1234567890123456789012345678",
            #         "0xOwner2Address1234567890123456789012345678", 
            #     ],
            #     threshold=2,
            #     master_copy="0xMasterCopyAddress1234567890123456789012",
            #     nonce=0,
            #     creation_block=0,  # Genesis block
            #     # creation_tx_hash will be auto-generated if not provided
            # ),
            # Add more SafeData instances as needed...
        ]
        # =================================================================
        
        if not SAFES_TO_INSERT:
            self.stdout.write(
                self.style.ERROR(
                    "No Safes configured for insertion. Please edit the command file "
                    "and add your Safe data to the SAFES_TO_INSERT list, or use --from-onchain."
                )
            )
            return

        # Process manually configured Safes
        self._process_safes(SAFES_TO_INSERT, dry_run)

    def _process_safes(self, safes_to_process: List[SafeData], dry_run: bool) -> None:
        """Process a list of SafeData objects."""
        self.stdout.write(f"Processing {len(safes_to_process)} Safes...")
        
        for i, safe_data in enumerate(safes_to_process, 1):
            self.stdout.write(f"\n--- Processing Safe {i}/{len(safes_to_process)} ---")
            self.stdout.write(f"Address: {safe_data.address}")
            self.stdout.write(f"Owners: {safe_data.owners}")
            self.stdout.write(f"Threshold: {safe_data.threshold}")
            
            try:
                if dry_run:
                    self._validate_safe_data(safe_data)
                    self.stdout.write(
                        self.style.SUCCESS(f"✓ Safe {safe_data.address} validation passed")
                    )
                else:
                    self._insert_safe(safe_data)
                    self.stdout.write(
                        self.style.SUCCESS(f"✓ Safe {safe_data.address} inserted successfully")
                    )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"✗ Failed to process Safe {safe_data.address}: {e}")
                )
                continue

        self.stdout.write(f"\nCompleted processing {len(safes_to_process)} Safes")

    def _display_safe_info(self, safe_data: SafeData) -> None:
        """Display detailed Safe information."""
        self.stdout.write(f"  Address: {safe_data.address}")
        self.stdout.write(f"  Owners ({len(safe_data.owners)}):")
        for i, owner in enumerate(safe_data.owners, 1):
            self.stdout.write(f"    {i}. {owner}")
        self.stdout.write(f"  Threshold: {safe_data.threshold}")
        self.stdout.write(f"  Master Copy: {safe_data.master_copy}")
        
        # Try to get version information
        try:
            version = SafeMasterCopy.objects.get_version_for_address(safe_data.master_copy)
            if version:
                self.stdout.write(f"  Master Copy Version: {version}")
        except:
            pass
            
        self.stdout.write(f"  Nonce: {safe_data.nonce}")
        self.stdout.write(f"  Fallback Handler: {safe_data.fallback_handler}")
        if safe_data.guard:
            self.stdout.write(f"  Guard: {safe_data.guard}")
        if safe_data.enabled_modules:
            self.stdout.write(f"  Enabled Modules ({len(safe_data.enabled_modules)}):")
            for i, module in enumerate(safe_data.enabled_modules, 1):
                self.stdout.write(f"    {i}. {module}")
        else:
            self.stdout.write(f"  Enabled Modules: None")

    def _validate_safe_data(self, safe_data: SafeData) -> None:
        """Validate Safe data without inserting into database."""
        # Check if Safe already exists
        if SafeContract.objects.filter(address=safe_data.address).exists():
            raise ValueError(f"Safe {safe_data.address} already exists in database")
        
        # Additional validations can be added here
        self.stdout.write(f"  Owners count: {len(safe_data.owners)}")
        self.stdout.write(f"  Threshold: {safe_data.threshold}")
        self.stdout.write(f"  Master copy: {safe_data.master_copy}")
        self.stdout.write(f"  Nonce: {safe_data.nonce}")
        self.stdout.write(f"  Fallback handler: {safe_data.fallback_handler}")
        if safe_data.guard:
            self.stdout.write(f"  Guard: {safe_data.guard}")
        if safe_data.enabled_modules:
            self.stdout.write(f"  Enabled modules: {safe_data.enabled_modules}")

    @transaction.atomic
    def _insert_safe(self, safe_data: SafeData) -> None:
        """Insert Safe data into database with proper transaction handling and SafeCreationView compatibility."""
        
        # Check if Safe already exists
        if SafeContract.objects.filter(address=safe_data.address).exists():
            raise ValueError(f"Safe {safe_data.address} already exists")

        # Generate setup data and factory call data for SafeCreationView compatibility
        setup_data = safe_data.generate_setup_data()
        factory_call_data = safe_data.generate_proxy_factory_call_data()
        
        # 1. Create or get EthereumBlock (genesis block)
        # Try to get existing block first, if it doesn't exist, create it with unique hashes
        try:
            genesis_block = EthereumBlock.objects.get(number=safe_data.creation_block)
        except EthereumBlock.DoesNotExist:
            # Create unique hashes based on block number and current time to avoid collisions
            import hashlib
            import time
            
            hash_input = f"block_{safe_data.creation_block}_{int(time.time() * 1000000)}".encode()
            block_hash = HexBytes(hashlib.sha256(hash_input + b"_block").digest())
            parent_hash = HexBytes(hashlib.sha256(hash_input + b"_parent").digest())
            
            genesis_block = EthereumBlock.objects.create(
                number=safe_data.creation_block,
                gas_limit=0,
                gas_used=0,
                timestamp=timezone.make_aware(datetime(2015, 7, 30, 15, 26, 13)),  # Ethereum genesis
                block_hash=block_hash,
                parent_hash=parent_hash,
                confirmed=True,
            )
        
        # 2. Create EthereumTx (main deployment transaction)
        ethereum_tx, created = EthereumTx.objects.get_or_create(
            tx_hash=HexBytes(safe_data.creation_tx_hash),
            defaults={
                "block": genesis_block,
                "gas_used": 0,
                "status": 1,  # Success
                "transaction_index": 0,
                "_from": safe_data.creator if safe_data.creator != "0x0000000000000000000000000000000000000000" else safe_data.owners[0],
                "gas": 0,
                "gas_price": 0,
                "nonce": 0,
                "to": safe_data.factory_address if safe_data.factory_address != "0x0000000000000000000000000000000000000000" else None,
                "value": 0,
                "type": 0,
                "data": factory_call_data,  # Proper proxy factory call data for SafeService._process_creation_data
            }
        )
        
        # 3. Create InternalTx for Safe contract creation (this is what SafeCreationView looks for)
        # Based on test_safe_service.py: creation trace needs trace_address="0"
        creation_internal_tx, created = InternalTx.objects.get_or_create(
            ethereum_tx=ethereum_tx,
            trace_address="0",  # Root trace for Safe creation
            defaults={
                "timestamp": genesis_block.timestamp,
                "block_number": safe_data.creation_block,
                "_from": safe_data.factory_address if safe_data.factory_address != "0x0000000000000000000000000000000000000000" else safe_data.owners[0],
                "gas": 0,
                "data": HexBytes("0x"),  # Creation bytecode would be here
                "to": None,
                "value": 0,
                "gas_used": 0,
                "contract_address": safe_data.address,  # Safe contract created
                "tx_type": InternalTxType.CREATE.value,
                "call_type": None,
                "error": None,
            }
        )
        
        # 4. Create InternalTx for Safe setup call (SafeCreationView needs this for setup data)
        # Based on test_safe_service.py: setup trace needs trace_address="0,0" (child of creation)
        if setup_data and setup_data != HexBytes("0x"):
            setup_internal_tx, created = InternalTx.objects.get_or_create(
                ethereum_tx=ethereum_tx,
                trace_address="0,0",  # Child of creation trace (0,0 means first child of trace 0)
                defaults={
                    "timestamp": genesis_block.timestamp,
                    "block_number": safe_data.creation_block,
                    "_from": safe_data.factory_address if safe_data.factory_address != "0x0000000000000000000000000000000000000000" else safe_data.owners[0],
                    "gas": 0,
                    "data": setup_data,  # This is the setup() call data
                    "to": safe_data.address,  # Setup call is made to the Safe
                    "value": 0,
                    "gas_used": 0,
                    "contract_address": None,  # Not a creation
                    "tx_type": InternalTxType.CALL.value,
                    "call_type": 0,  # CALL
                    "error": None,
                }
            )
        
        # 5. Create SafeContract
        safe_contract, created = SafeContract.objects.get_or_create(
            address=safe_data.address,
            defaults={
                "ethereum_tx": ethereum_tx,
                "banned": False,
            }
        )
        
        # 6. Create SafeLastStatus
        safe_last_status, created = SafeLastStatus.objects.update_or_create(
            address=safe_data.address,
            defaults={
                "internal_tx": creation_internal_tx,
                "owners": safe_data.owners,
                "threshold": safe_data.threshold,
                "nonce": safe_data.nonce,
                "master_copy": safe_data.master_copy,
                "fallback_handler": safe_data.fallback_handler,
                "guard": safe_data.guard,
                "enabled_modules": safe_data.enabled_modules,
            }
        )
        
        self.stdout.write(f"  Created EthereumTx: {ethereum_tx.tx_hash.hex()}")
        self.stdout.write(f"  Created creation InternalTx: {creation_internal_tx.id}")
        if setup_data and setup_data != HexBytes("0x"):
            self.stdout.write(f"  Created setup InternalTx for SafeCreationView compatibility")
        self.stdout.write(f"  Created SafeContract: {safe_contract.address}")
        self.stdout.write(f"  Created SafeLastStatus: {safe_last_status.address}")
