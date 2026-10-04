from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.db.models import Sum, Q
import random
import string
import uuid
from decimal import Decimal
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.core.exceptions import ValidationError
from datetime import timedelta


# ==================== BASE MODELS ====================

class TimeStampedModel(models.Model):
    """
    Abstract base model that provides self-updating 'created_at' and 'updated_at' fields.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ==================== BUSINESS DAY UTILITY FUNCTIONS ====================

def is_weekday(date=None):
    """
    Check if a given date is a weekday (Monday-Friday)
    Returns: True if weekday, False if weekend
    """
    if date is None:
        date = timezone.now()
    return date.weekday() < 5


def is_weekend(date=None):
    """
    Check if a given date is a weekend (Saturday-Sunday)
    Returns: True if weekend, False if weekday
    """
    if date is None:
        date = timezone.now()
    return date.weekday() >= 5


def count_business_days(start_date, end_date):
    """
    Count business days (Monday-Friday) between two dates
    """
    if start_date > end_date:
        return 0

    # Convert to date objects for comparison
    start = start_date.date() if hasattr(start_date, 'date') else start_date
    end = end_date.date() if hasattr(end_date, 'date') else end_date

    # If same day, count if it's a weekday
    if start == end:
        return 1 if start.weekday() < 5 else 0

    # Count business days
    business_days = 0
    current = start

    while current <= end:
        if current.weekday() < 5:  # Monday=0, Friday=4
            business_days += 1
        current += timedelta(days=1)

    return business_days


def get_next_business_day(date=None):
    """
    Get the next business day (Monday-Friday)
    """
    if date is None:
        date = timezone.now()

    next_day = date + timedelta(days=1)

    # Keep adding days until we hit a weekday
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)

    return next_day


def get_business_days_add(date, days_to_add):
    """
    Add business days to a date (skip weekends)
    Returns: datetime of the date after adding business days
    """
    if days_to_add <= 0:
        return date

    current_date = date
    business_days_added = 0

    while business_days_added < days_to_add:
        current_date += timedelta(days=1)
        # Only count if it's a weekday (Monday-Friday)
        if current_date.weekday() < 5:
            business_days_added += 1

    return current_date


class UserProfile(TimeStampedModel):
    """
    Extended user profile with referral and verification system.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    phone_number = models.CharField(max_length=20, db_index=True)
    national_id_name = models.CharField(max_length=100, blank=True, null=True)
    referral_code = models.CharField(max_length=10, unique=True, blank=True)
    referred_by = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals')
    phone_verified = models.BooleanField(default=False)
    id_verified = models.BooleanField(default=False)

    # KYC Documents
    id_front_image = models.ImageField(upload_to='kyc/ids/front/', blank=True, null=True)
    id_back_image = models.ImageField(upload_to='kyc/ids/back/', blank=True, null=True)
    selfie_with_id = models.ImageField(upload_to='kyc/selfies/', blank=True, null=True)

    # Account status
    is_active = models.BooleanField(default=True)
    is_banned = models.BooleanField(default=False)
    ban_reason = models.TextField(blank=True, null=True)

    # Notification preferences
    email_notifications = models.BooleanField(default=True)
    sms_notifications = models.BooleanField(default=True)

    # Chat/Online status
    online_status = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    away_mode = models.BooleanField(default=False)

    # Chat preferences
    chat_notifications = models.BooleanField(default=True)
    chat_sounds = models.BooleanField(default=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['referral_code']),
            models.Index(fields=['phone_number']),
            models.Index(fields=['-created_at']),
            models.Index(fields=['online_status', 'last_seen']),
        ]

    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = self.generate_referral_code()
        super().save(*args, **kwargs)

    def generate_referral_code(self):
        """Generate unique referral code"""
        while True:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            if not UserProfile.objects.filter(referral_code=code).exists():
                return code

    def get_referral_count(self):
        """Get total number of direct referrals"""
        return self.referrals.count()

    def get_total_referral_earnings(self):
        """Calculate total earnings from referrals"""
        return self.user.wallet.transactions.filter(
            transaction_type='REFERRAL_BONUS',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    def get_total_deposits(self):
        """Get total deposits made by user"""
        return self.user.wallet.transactions.filter(
            transaction_type='DEPOSIT',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    def __str__(self):
        return f"{self.user.username} - {self.referral_code}"


# ==================== WALLET & TRANSACTIONS ====================

class Wallet(TimeStampedModel):
    """
    User's wallet for managing funds.
    - balance: Available money the user can withdraw or invest (deposits + profits - withdrawals - investments)
    - locked_balance: Money currently active in investments (cannot be withdrawn until investment completes)
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('0.00'))  # Available balance
    locked_balance = models.DecimalField(max_digits=20, decimal_places=2,
                                         default=Decimal('0.00'))  # Funds in active investments
    total_deposited = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('0.00'))
    total_withdrawn = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('0.00'))
    total_earned = models.DecimalField(max_digits=20, decimal_places=2,
                                       default=Decimal('0.00'))  # Profits + referral bonuses

    # Currency (default KSH)
    currency = models.CharField(max_length=3, default='KSH')

    class Meta:
        indexes = [
            models.Index(fields=['user', 'balance']),
        ]

    def available_balance(self):
        """Balance available for withdrawal/investment (should never be negative)"""
        return self.balance

    def get_breakdown(self):
        """Get wallet breakdown for display"""
        return {
            'available_balance': self.balance,
            'locked_in_investments': self.locked_balance,
            'total_balance': self.balance + self.locked_balance,
            'total_deposited': self.total_deposited,
            'total_withdrawn': self.total_withdrawn,
            'total_profits_earned': self.total_earned,
        }

    def can_invest(self, amount):
        """Check if user has enough available balance to invest"""
        return self.balance >= amount

    def can_withdraw(self, amount):
        """Check if user has enough available balance to withdraw"""
        return self.balance >= amount

    def update_balances(self):
        """Recalculate balances from transactions (admin use only)"""
        deposits = self.transactions.filter(
            transaction_type='DEPOSIT',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        withdrawals = self.transactions.filter(
            transaction_type='WITHDRAWAL',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        earnings = self.transactions.filter(
            transaction_type='PROFIT',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

        self.total_deposited = deposits
        self.total_withdrawn = withdrawals
        self.total_earned = earnings
        self.save()

    def __str__(self):
        return f"{self.user.username}'s Wallet - Available: {self.balance} {self.currency} | Locked: {self.locked_balance} {self.currency}"


class Transaction(TimeStampedModel):
    """
    All financial transactions in the system.
    """
    TRANSACTION_TYPES = [
        ('DEPOSIT', 'Deposit'),
        ('WITHDRAWAL', 'Withdrawal'),
        ('INVESTMENT', 'Investment'),
        ('PROFIT', 'Profit'),
        ('REFERRAL_BONUS', 'Referral Bonus'),
        ('PENALTY', 'Penalty'),
        ('ADJUSTMENT', 'Credited'),
    ]

    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
        ('PROCESSING', 'Processing'),
    ]

    transaction_id = models.CharField(max_length=50, unique=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    description = models.CharField(max_length=255)

    # For related objects
    investment = models.ForeignKey('Investment', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='transaction_entries')
    withdrawal = models.ForeignKey('WithdrawalRequest', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='transaction_entries')

    # Metadata
    processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='processed_transactions')
    processed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['transaction_id']),
            models.Index(fields=['wallet', 'status']),
            models.Index(fields=['wallet', 'transaction_type']),
            models.Index(fields=['-created_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.transaction_id:
            self.transaction_id = f"TXN{timezone.now().strftime('%Y%m%d%H%M%S')}{random.randint(1000, 9999)}"
        super().save(*args, **kwargs)

    def process(self, admin_user=None):
        """Process a pending transaction"""
        if self.status != 'PENDING':
            raise ValidationError("Can only process pending transactions")

        self.status = 'COMPLETED'
        self.processed_by = admin_user
        self.processed_at = timezone.now()

        # Update wallet balance based on transaction type
        if self.transaction_type in ['DEPOSIT', 'PROFIT', 'REFERRAL_BONUS']:
            self.wallet.balance += self.amount
        elif self.transaction_type == 'WITHDRAWAL':
            if self.wallet.balance < self.amount:
                raise ValidationError("Insufficient available balance")
            self.wallet.balance -= self.amount
        elif self.transaction_type == 'INVESTMENT':
            # Investment is handled in Investment model save method
            pass

        self.wallet.save()
        self.save()

    def __str__(self):
        return f"{self.transaction_id} - {self.transaction_type} - {self.amount}"


# ==================== MPESA INTEGRATION ====================

class MpesaPayment(TimeStampedModel):
    """
    M-Pesa payment tracking and verification.
    """
    PAYMENT_STATUS = [
        ('PENDING', 'Pending Verification'),
        ('VERIFIED', 'Verified'),
        ('REJECTED', 'Rejected'),
        ('PROCESSING', 'Processing'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mpesa_payments')
    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE, related_name='mpesa_payment', null=True,
                                       blank=True)

    # Payment details
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal('800.00'))])
    phone_number = models.CharField(max_length=15)  # Sender's phone

    # M-Pesa message/screenshot
    mpesa_message = models.TextField(help_text="Full M-Pesa confirmation message")
    mpesa_screenshot = models.ImageField(upload_to='mpesa/screenshots/%Y/%m/%d/', blank=True, null=True)

    # Extracted data from message
    mpesa_code = models.CharField(max_length=50, blank=True, null=True, db_index=True,
                                  help_text="Extracted M-Pesa transaction code")
    transaction_date = models.DateTimeField(blank=True, null=True)
    sender_name = models.CharField(max_length=100, blank=True, null=True)

    # Status
    status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='PENDING', db_index=True)
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='verified_payments')
    verified_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, null=True)

    # Admin notes
    admin_notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['mpesa_code']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['-created_at']),
        ]

    def extract_mpesa_data(self):
        """Extract transaction code, date, and sender from M-Pesa message."""
        import re
        from datetime import datetime

        message = self.mpesa_message

        # Extract M-Pesa confirmation code
        code_patterns = [
            r'([A-Z0-9]{10,12})',
            r'Confirmation\.?([A-Z0-9]+)',
            r'([A-Z0-9]+)\s+Confirmed',
        ]

        for pattern in code_patterns:
            match = re.search(pattern, message)
            if match:
                self.mpesa_code = match.group(1)
                break

        # Extract amount
        amount_pattern = r'(?:Ksh|KES|KSh)[\s.]*([0-9,]+(?:\.[0-9]{2})?)'
        amount_match = re.search(amount_pattern, message, re.IGNORECASE)
        if amount_match:
            amount_str = amount_match.group(1).replace(',', '')
            extracted_amount = Decimal(amount_str)
            if abs(extracted_amount - self.amount) > Decimal('1.00'):
                self.admin_notes = f"Amount mismatch: Message shows {extracted_amount}"

        # Extract sender phone
        phone_pattern = r'(?:from|sender)[\s:]*0?(\d{9,12})'
        phone_match = re.search(phone_pattern, message, re.IGNORECASE)
        if phone_match:
            self.sender_name = phone_match.group(1)

        self.save()

    def verify(self, admin_user):
        """Admin verification of M-Pesa payment"""
        if self.status != 'PENDING':
            raise ValidationError("Can only verify pending payments")

        self.status = 'VERIFIED'
        self.verified_by = admin_user
        self.verified_at = timezone.now()
        self.save()

        # Create deposit transaction
        transaction = Transaction.objects.create(
            wallet=self.user.wallet,
            transaction_type='DEPOSIT',
            amount=self.amount,
            description=f"M-Pesa deposit - {self.mpesa_code or 'Manual verification'}",
            status='COMPLETED'
        )
        transaction.processed_by = admin_user
        transaction.processed_at = timezone.now()
        transaction.save()

        # Link to this payment
        self.transaction = transaction
        self.save()

        # Update wallet - add to available balance
        wallet = self.user.wallet
        wallet.balance += self.amount
        wallet.total_deposited += self.amount
        wallet.save()

        # Process first deposit referral bonus
        if wallet.total_deposited == self.amount:
            self.process_referral_bonus()

        return transaction

    def process_referral_bonus(self):
        """Give 5% referral bonus to referrer on first deposit"""
        profile = self.user.profile
        if profile.referred_by:
            referrer = profile.referred_by.user
            bonus_amount = self.amount * Decimal('0.05')

            Transaction.objects.create(
                wallet=referrer.wallet,
                transaction_type='REFERRAL_BONUS',
                amount=bonus_amount,
                description=f"5% referral bonus from {self.user.username}'s first deposit",
                status='COMPLETED'
            )

            referrer.wallet.balance += bonus_amount
            referrer.wallet.total_earned += bonus_amount
            referrer.wallet.save()

    def reject(self, admin_user, reason):
        """Reject a payment"""
        self.status = 'REJECTED'
        self.verified_by = admin_user
        self.verified_at = timezone.now()
        self.rejection_reason = reason
        self.save()

    def __str__(self):
        return f"M-Pesa {self.amount} - {self.user.username} - {self.status}"


# ==================== INVESTMENT TOKENS ====================

class Token(TimeStampedModel):
    """
    Investment token definitions (XMR-1 to XMR-20)
    """
    TOKEN_STATUS = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('COMING_SOON', 'Coming Soon'),
        ('SOLD_OUT', 'Sold Out'),
    ]

    name = models.CharField(max_length=20, unique=True)
    display_name = models.CharField(max_length=50)
    token_number = models.IntegerField(unique=True, validators=[MinValueValidator(1), MaxValueValidator(20)])

    # Investment details
    minimum_investment = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('800.00'))
    daily_return = models.DecimalField(max_digits=10, decimal_places=2, help_text="Daily return in KSH")
    return_days = models.IntegerField(default=12, help_text="Number of business days returns are paid")
    total_return = models.DecimalField(max_digits=20, decimal_places=2, editable=False)

    # Token status
    status = models.CharField(max_length=20, choices=TOKEN_STATUS, default='ACTIVE', db_index=True)

    # Limits
    max_purchases_per_user = models.IntegerField(default=1, help_text="Maximum times a user can buy this token")
    total_supply = models.IntegerField(null=True, blank=True, help_text="Total available tokens, null for unlimited")
    purchased_count = models.IntegerField(default=0, editable=False)

    # Metadata
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True, help_text="FontAwesome or custom icon class")
    color = models.CharField(max_length=20, default='primary', help_text="Bootstrap color class")

    class Meta:
        ordering = ['token_number']
        indexes = [
            models.Index(fields=['status', 'token_number']),
        ]

    def save(self, *args, **kwargs):
        self.total_return = self.daily_return * self.return_days
        if self.token_number > 10:
            self.status = 'INACTIVE'
        super().save(*args, **kwargs)

    def is_available(self):
        """Check if token is available for purchase"""
        if self.status != 'ACTIVE':
            return False
        if self.total_supply and self.purchased_count >= self.total_supply:
            return False
        return True

    def get_roi_percentage(self):
        """Calculate ROI percentage"""
        return (self.total_return / self.minimum_investment) * 100

    def __str__(self):
        return f"{self.name} - {self.daily_return} KSH/day for {self.return_days} business days"


class Investment(TimeStampedModel):
    """
    User's investment in a token - Uses BUSINESS DAYS for all calculations
    """
    INVESTMENT_STATUS = [
        ('ACTIVE', 'Active'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    ]

    investment_id = models.CharField(max_length=50, unique=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='investments')
    token = models.ForeignKey(Token, on_delete=models.PROTECT, related_name='investments')

    # Investment details
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal('800.00'))])
    daily_return = models.DecimalField(max_digits=10, decimal_places=2)

    # Dates
    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField()
    last_payout_date = models.DateTimeField(null=True, blank=True)

    # Status
    status = models.CharField(max_length=20, choices=INVESTMENT_STATUS, default='ACTIVE', db_index=True)

    # Returns tracking
    total_paid = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('0.00'))
    remaining_payouts = models.IntegerField()

    # Related transaction
    transaction = models.OneToOneField(Transaction, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='investment_purchase')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['end_date', 'status']),
            models.Index(fields=['investment_id']),
        ]

    # ===== BUSINESS DAY HELPER METHODS =====

    def calculate_end_date(self):
        """
        Calculate end date based on business days only (Monday-Friday)
        Returns: datetime of the last business day
        """
        start = self.start_date
        days_remaining = self.token.return_days
        current_date = start

        # Count forward only business days
        business_days_counted = 0

        while business_days_counted < days_remaining:
            current_date += timedelta(days=1)
            # Only count if it's a weekday (Monday-Friday)
            if current_date.weekday() < 5:
                business_days_counted += 1

        return current_date

    def get_business_days_remaining(self, from_date=None):
        """
        Calculate business days remaining until end_date
        Returns: integer number of business days remaining
        """
        if from_date is None:
            from_date = timezone.now()

        if from_date >= self.end_date:
            return 0

        # Count business days between now and end_date
        current = from_date.date()
        end = self.end_date.date()
        business_days = 0

        while current <= end:
            if current.weekday() < 5:  # Monday-Friday
                business_days += 1
            current += timedelta(days=1)

        return business_days

    def get_next_payout_date(self):
        """
        Get the next business day payout date
        Returns: datetime of next payout (always a weekday)
        """
        now = timezone.now()

        # If no payouts yet, next payout is 24 business hours after creation
        if not self.last_payout_date:
            next_date = self.created_at + timedelta(days=1)
        else:
            next_date = self.last_payout_date + timedelta(days=1)

        # If it's a weekend, move to next Monday
        while next_date.weekday() >= 5:  # Saturday (5) or Sunday (6)
            next_date += timedelta(days=1)

        return next_date

    def get_business_days_until_payout(self):
        """
        Get business days until next payout
        Returns: tuple (days, hours, minutes)
        """
        next_payout = self.get_next_payout_date()
        now = timezone.now()

        if next_payout <= now:
            return (0, 0, 0)

        # Calculate difference
        diff = next_payout - now
        total_seconds = diff.total_seconds()

        days = int(total_seconds // (24 * 3600))
        hours = int((total_seconds % (24 * 3600)) // 3600)
        minutes = int((total_seconds % 3600) // 60)

        return (days, hours, minutes)

    # ===== SAVE METHOD =====

    def save(self, *args, **kwargs):
        if not self.pk:  # New investment
            # Check if token is still available
            if not self.token.is_available():
                raise ValidationError("This token is no longer available")

            self.daily_return = self.token.daily_return

            # === FIXED: Calculate end date using BUSINESS DAYS ===
            self.start_date = timezone.now()
            self.end_date = self.calculate_end_date()
            self.remaining_payouts = self.token.return_days

            # Lock the funds in wallet
            wallet = self.user.wallet

            # Check if user has enough available balance
            if wallet.balance < self.amount:
                raise ValidationError(f"Insufficient available balance. You have {wallet.balance} KSH available.")

            # MOVE money from available to locked
            wallet.balance -= self.amount
            wallet.locked_balance += self.amount
            wallet.save()

            # Save investment first to get an ID
            super().save(*args, **kwargs)

            # Create transaction with the saved investment
            transaction = Transaction.objects.create(
                wallet=wallet,
                transaction_type='INVESTMENT',
                amount=self.amount,
                description=f"Investment in {self.token.name}",
                status='COMPLETED',
                investment=self
            )

            # Update investment with transaction reference
            self.transaction = transaction
            super().save(update_fields=['transaction'])

            # Update token purchase count
            self.token.purchased_count += 1
            self.token.save()

        else:  # Existing investment
            super().save(*args, **kwargs)

    # ===== PAYOUT METHODS =====

    def process_daily_payout(self):
        """
        Process a single day's payout - PROFIT ONLY, no principal unlocking
        Only processes if it's a WEEKDAY (Monday-Friday)
        """
        # === FIXED: Weekend check ===
        now = timezone.now()
        if is_weekend(now):
            # Don't process on weekends
            return False

        if self.status != 'ACTIVE':
            return False

        if self.remaining_payouts <= 0:
            self.complete_investment()
            return False

        # Check if enough business days have passed
        if self.last_payout_date:
            # Count business days since last payout
            business_days_passed = count_business_days(self.last_payout_date, now)
        else:
            # First payout - count business days since creation
            business_days_passed = count_business_days(self.created_at, now)

        # Only process if at least ONE business day has passed
        if business_days_passed < 1:
            return False

        # Calculate payout (profit only - principal stays locked)
        profit_amount = self.daily_return

        # Create profit transaction
        transaction = Transaction.objects.create(
            wallet=self.user.wallet,
            transaction_type='PROFIT',
            amount=profit_amount,
            description=f"Daily profit from {self.token.name}",
            status='COMPLETED',
            investment=self
        )

        # Update wallet - ONLY ADD PROFIT to available balance
        wallet = self.user.wallet
        wallet.balance += profit_amount
        wallet.total_earned += profit_amount
        wallet.save()

        # Update investment
        self.total_paid += profit_amount
        self.remaining_payouts -= 1
        self.last_payout_date = now

        if self.remaining_payouts <= 0:
            self.complete_investment()
        else:
            self.save()

        return True

    def check_and_process_payout(self):
        """
        Check if payout is due and process if needed
        Only processes on weekdays
        """
        now = timezone.now()

        # === FIXED: Weekend check ===
        if is_weekend(now):
            return False

        if self.status != 'ACTIVE' or self.remaining_payouts <= 0:
            return False

        # Check if enough business days have passed
        if self.last_payout_date:
            business_days_passed = count_business_days(self.last_payout_date, now)
        else:
            business_days_passed = count_business_days(self.created_at, now)

        if business_days_passed >= 1:
            return self.process_daily_payout()

        return False

    def complete_investment(self):
        """Mark investment as completed - RETURN THE PRINCIPAL to available balance"""
        self.status = 'COMPLETED'

        # Return the FULL principal to available balance
        wallet = self.user.wallet
        wallet.balance += self.amount
        wallet.locked_balance -= self.amount
        wallet.save()

        self.save()

    def get_progress_percentage(self):
        """Calculate investment progress based on business days"""
        total_days = self.token.return_days
        completed_days = total_days - self.remaining_payouts
        return (completed_days / total_days) * 100 if total_days > 0 else 0

    def get_formatted_end_date(self):
        """Get end date formatted for display"""
        return self.end_date.strftime('%A, %B %d, %Y')

    def get_business_days_elapsed(self):
        """Get number of business days elapsed since start"""
        return count_business_days(self.start_date, timezone.now())

    def __str__(self):
        return f"{self.investment_id} - {self.user.username} - {self.token.name}"


# ==================== WITHDRAWAL SYSTEM ====================

class WithdrawalRequest(TimeStampedModel):
    """
    User withdrawal requests - ONLY affects available balance, never locked balance
    """
    WITHDRAWAL_STATUS = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('REJECTED', 'Rejected'),
        ('CANCELLED', 'Cancelled'),
    ]

    PAYMENT_METHODS = [
        ('MPESA', 'M-Pesa'),
        ('BANK', 'Bank Transfer'),
    ]

    request_id = models.CharField(max_length=50, unique=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='withdrawals')

    # Amount
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal('200.00'))])
    tax_amount = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal('0.00'))
    net_amount = models.DecimalField(max_digits=20, decimal_places=2)

    # Payment details
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='MPESA')
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    bank_details = models.JSONField(blank=True, null=True)

    # Status
    status = models.CharField(max_length=20, choices=WITHDRAWAL_STATUS, default='PENDING', db_index=True)

    # Processing
    processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='processed_withdrawals')
    processed_at = models.DateTimeField(null=True, blank=True)
    transaction_code = models.CharField(max_length=50, blank=True, null=True)

    # Rejection
    rejection_reason = models.TextField(blank=True, null=True)

    # Admin notes
    admin_notes = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['request_id']),
        ]

    def save(self, *args, **kwargs):
        if not self.pk:  # New withdrawal request
            # Calculate 5% tax
            self.tax_amount = self.amount * Decimal('0.05')
            self.net_amount = self.amount - self.tax_amount

            # Check minimum withdrawal
            if self.amount < 200:
                raise ValidationError("Minimum withdrawal amount is 200 KSH")

            wallet = self.user.wallet

            # Check available balance (not locked balance)
            if wallet.balance < self.amount:
                raise ValidationError(
                    f"Insufficient available balance. You have {wallet.balance} KSH available, but requested {self.amount} KSH.")

        super().save(*args, **kwargs)

    def process(self, admin_user, transaction_code=None):
        """Approve and process withdrawal"""
        if self.status != 'PENDING':
            raise ValidationError("Can only process pending withdrawals")

        self.status = 'PROCESSING'
        self.processed_by = admin_user
        self.processed_at = timezone.now()
        self.transaction_code = transaction_code
        self.save()

    def complete(self, admin_user, transaction_code):
        """Mark withdrawal as completed - DEDUCT FROM AVAILABLE BALANCE ONLY"""
        if self.status not in ['PENDING', 'PROCESSING']:
            raise ValidationError("Can only complete pending or processing withdrawals")

        wallet = self.user.wallet

        # Check available balance again
        if wallet.balance < self.amount:
            raise ValidationError(f"Insufficient available balance. Current balance: {wallet.balance} KSH")

        # Create withdrawal transaction
        transaction = Transaction.objects.create(
            wallet=wallet,
            transaction_type='WITHDRAWAL',
            amount=self.amount,
            description=f"Withdrawal via {self.payment_method}",
            status='COMPLETED',
            withdrawal=self
        )
        transaction.processed_by = admin_user
        transaction.processed_at = timezone.now()
        transaction.save()

        # Update wallet - ONLY deduct from available balance
        wallet.balance -= self.amount
        wallet.total_withdrawn += self.amount
        wallet.save()

        # Add tax as separate transaction or record
        if self.tax_amount > 0:
            Transaction.objects.create(
                wallet=wallet,
                transaction_type='PENALTY',
                amount=self.tax_amount,
                description=f"5% withdrawal tax",
                status='COMPLETED'
            )

        self.status = 'COMPLETED'
        self.transaction_code = transaction_code
        self.save()

    def reject(self, admin_user, reason):
        """Reject withdrawal request - NO FUNDS TO UNLOCK"""
        if self.status not in ['PENDING', 'PROCESSING']:
            raise ValidationError("Can only reject pending or processing withdrawals")

        self.status = 'REJECTED'
        self.processed_by = admin_user
        self.processed_at = timezone.now()
        self.rejection_reason = reason
        self.save()

    def cancel(self):
        """User cancels withdrawal request - NO FUNDS TO UNLOCK"""
        if self.status != 'PENDING':
            raise ValidationError("Can only cancel pending withdrawals")

        self.status = 'CANCELLED'
        self.save()

    def __str__(self):
        return f"{self.request_id} - {self.user.username} - {self.amount} KSH"


# ==================== SYSTEM CONFIGURATION ====================

class SystemConfig(models.Model):
    """
    Global system configuration
    """
    key = models.CharField(max_length=50, unique=True, db_index=True)
    value = models.JSONField()
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "System Configuration"
        verbose_name_plural = "System Configurations"

    @classmethod
    def get_config(cls, key, default=None):
        """Get configuration value"""
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default

    def __str__(self):
        return self.key


class SystemLog(TimeStampedModel):
    """
    System-wide logging for important events
    """
    LOG_TYPES = [
        ('INFO', 'Information'),
        ('WARNING', 'Warning'),
        ('ERROR', 'Error'),
        ('CRITICAL', 'Critical'),
        ('ADMIN_ACTION', 'Admin Action'),
    ]

    log_type = models.CharField(max_length=20, choices=LOG_TYPES, db_index=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    description = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    data = models.JSONField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['log_type', '-created_at']),
            models.Index(fields=['user', '-created_at']),
        ]

    def __str__(self):
        return f"{self.log_type} - {self.action} - {self.created_at}"


# ==================== SIGNALS ====================

@receiver(post_save, sender=User)
def create_user_wallet_and_profile(sender, instance, created, **kwargs):
    """Create wallet and profile for new users"""
    if created:
        UserProfile.objects.get_or_create(user=instance)
        Wallet.objects.get_or_create(user=instance)
    else:
        UserProfile.objects.get_or_create(
            user=instance,
            defaults={
                'phone_number': '',
                'national_id_name': instance.get_full_name() or instance.username
            }
        )
        Wallet.objects.get_or_create(user=instance)


@receiver(post_save, sender=Investment)
def update_token_purchased_count(sender, instance, created, **kwargs):
    """Update token purchased count when investment is made"""
    if created:
        token = instance.token
        token.purchased_count = Investment.objects.filter(token=token, status='ACTIVE').count()
        token.save()


# ==================== ADMIN INTERFACE HELPERS ====================

class AdminDashboard:
    """Helper class for admin dashboard statistics"""

    @staticmethod
    def get_dashboard_stats():
        """Get comprehensive dashboard statistics"""
        from django.db.models import Sum, Count, Avg

        today = timezone.now().date()
        week_ago = today - timezone.timedelta(days=7)

        return {
            'users': {
                'total': User.objects.count(),
                'new_today': User.objects.filter(date_joined__date=today).count(),
                'new_week': User.objects.filter(date_joined__date__gte=week_ago).count(),
                'verified': UserProfile.objects.filter(phone_verified=True).count(),
            },
            'transactions': {
                'total_deposits': Transaction.objects.filter(
                    transaction_type='DEPOSIT',
                    status='COMPLETED'
                ).aggregate(total=Sum('amount'))['total'] or 0,
                'total_withdrawals': Transaction.objects.filter(
                    transaction_type='WITHDRAWAL',
                    status='COMPLETED'
                ).aggregate(total=Sum('amount'))['total'] or 0,
                'pending_deposits': MpesaPayment.objects.filter(status='PENDING').count(),
                'pending_withdrawals': WithdrawalRequest.objects.filter(status='PENDING').count(),
            },
            'investments': {
                'active': Investment.objects.filter(status='ACTIVE').count(),
                'total_invested': Investment.objects.filter(
                    status='ACTIVE'
                ).aggregate(total=Sum('amount'))['total'] or 0,
                'total_paid_out': Investment.objects.aggregate(
                    total=Sum('total_paid')
                )['total'] or 0,
                'by_token': Token.objects.annotate(
                    active_count=Count('investments', filter=Q(investments__status='ACTIVE'))
                ).values('name', 'active_count'),
            },
            'referrals': {
                'total': UserProfile.objects.filter(referred_by__isnull=False).count(),
                'total_commission': Transaction.objects.filter(
                    transaction_type='REFERRAL_BONUS'
                ).aggregate(total=Sum('amount'))['total'] or 0,
            },
            'system_balances': {
                'total_available_balance': Wallet.objects.aggregate(total=Sum('balance'))['total'] or 0,
                'total_locked_balance': Wallet.objects.aggregate(total=Sum('locked_balance'))['total'] or 0,
                'total_system_assets': Wallet.objects.aggregate(
                    total=Sum('balance') + Sum('locked_balance')
                )['total'] or 0,
            }
        }


# ==================== CHAT SYSTEM MODELS ====================

class ChatRoom(TimeStampedModel):
    """
    Chat room model for group conversations.
    """
    ROOM_TYPES = [
        ('PUBLIC', 'Public Room'),
        ('PRIVATE', 'Private Room'),
        ('DIRECT', 'Direct Message'),
        ('INVESTMENT', 'Investment Discussion'),
        ('SUPPORT', 'Support Chat'),
    ]

    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, unique=True, blank=True)
    room_type = models.CharField(max_length=20, choices=ROOM_TYPES, default='PUBLIC', db_index=True)
    description = models.TextField(blank=True, max_length=500)

    # Relationships
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_chat_rooms')
    participants = models.ManyToManyField(User, related_name='chat_rooms', blank=True)
    moderators = models.ManyToManyField(User, related_name='moderated_rooms', blank=True)
    banned_users = models.ManyToManyField(User, related_name='banned_from_rooms', blank=True)

    # Settings
    is_active = models.BooleanField(default=True)
    is_protected = models.BooleanField(default=False, help_text="Requires approval to join")
    max_participants = models.IntegerField(default=100, validators=[MinValueValidator(2), MaxValueValidator(1000)])
    password = models.CharField(max_length=128, blank=True, null=True,
                                help_text="Optional password for protected rooms")

    # Metadata
    icon = models.CharField(max_length=50, blank=True, help_text="FontAwesome or emoji icon")
    color = models.CharField(max_length=20, default='primary')
    last_activity = models.DateTimeField(auto_now=True)

    # Stats
    total_messages = models.IntegerField(default=0, editable=False)
    online_count = models.IntegerField(default=0, editable=False)

    class Meta:
        ordering = ['-last_activity', 'name']
        indexes = [
            models.Index(fields=['slug']),
            models.Index(fields=['room_type', 'is_active']),
            models.Index(fields=['-last_activity']),
        ]
        verbose_name = "Chat Room"
        verbose_name_plural = "Chat Rooms"

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while ChatRoom.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def add_participant(self, user):
        """Add user to room participants"""
        if user not in self.participants.all() and user not in self.banned_users.all():
            self.participants.add(user)
            self.log_activity(user, 'JOINED')
            return True
        return False

    def remove_participant(self, user):
        """Remove user from room participants"""
        if user in self.participants.all():
            self.participants.remove(user)
            self.log_activity(user, 'LEFT')
            return True
        return False

    def ban_user(self, user, moderator, reason=""):
        """Ban user from room"""
        if user in self.participants.all():
            self.participants.remove(user)
        self.banned_users.add(user)
        self.log_activity(moderator, 'BAN', target_user=user, reason=reason)

    def unban_user(self, user, moderator):
        """Unban user from room"""
        self.banned_users.remove(user)
        self.log_activity(moderator, 'UNBAN', target_user=user)

    def make_moderator(self, user, admin_user):
        """Make user a moderator"""
        if user in self.participants.all():
            self.moderators.add(user)
            self.log_activity(admin_user, 'MOD_ADD', target_user=user)

    def remove_moderator(self, user, admin_user):
        """Remove moderator status"""
        self.moderators.remove(user)
        self.log_activity(admin_user, 'MOD_REMOVE', target_user=user)

    def log_activity(self, user, action, target_user=None, reason=""):
        """Log room activity"""
        ChatActivity.objects.create(
            room=self,
            user=user,
            action=action,
            target_user=target_user,
            reason=reason
        )

    def can_join(self, user):
        """Check if user can join room"""
        if user in self.banned_users.all():
            return False, "You are banned from this room"
        if self.participants.count() >= self.max_participants:
            return False, "Room is full"
        if self.is_protected and self.password:
            return False, "Password required"
        return True, "Allowed"

    def get_online_users(self):
        """Get currently online users in this room"""
        return self.participants.filter(
            profile__online_status=True
        )[:50]

    def update_online_count(self):
        """Update online count (called by WebSocket)"""
        self.online_count = self.participants.filter(
            profile__online_status=True
        ).count()
        self.save(update_fields=['online_count'])

    def __str__(self):
        return f"{self.name} ({self.room_type})"


class ChatMessage(TimeStampedModel):
    """
    Individual chat messages with rich features.
    """
    MESSAGE_TYPES = [
        ('TEXT', 'Text Message'),
        ('SYSTEM', 'System Message'),
        ('JOIN', 'User Joined'),
        ('LEAVE', 'User Left'),
        ('INVESTMENT', 'Investment Update'),
        ('PAYOUT', 'Payout Notification'),
        ('ALERT', 'Alert'),
        ('IMAGE', 'Image'),
        ('FILE', 'File'),
    ]

    message_id = models.CharField(max_length=50, unique=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='chat_messages')

    # Message content
    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES, default='TEXT', db_index=True)
    content = models.TextField(blank=True)

    # Rich content
    image = models.ImageField(upload_to='chat/images/%Y/%m/%d/', blank=True, null=True)
    file = models.FileField(upload_to='chat/files/%Y/%m/%d/', blank=True, null=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.IntegerField(blank=True, null=True)

    # Metadata
    is_edited = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)
    is_announcement = models.BooleanField(default=False)

    # Reply to
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')

    # Mentions
    mentions = models.ManyToManyField(User, related_name='mentioned_in_messages', blank=True)

    # Read receipts
    read_by = models.ManyToManyField(User, related_name='read_messages', blank=True)

    # Investment integration
    investment = models.ForeignKey(Investment, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='chat_messages')
    transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='chat_messages')

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['room', '-created_at']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['message_type', 'created_at']),
            models.Index(fields=['is_pinned', 'room']),
        ]

    def save(self, *args, **kwargs):
        if not self.pk:
            ChatRoom.objects.filter(pk=self.room.pk).update(
                total_messages=models.F('total_messages') + 1
            )
        super().save(*args, **kwargs)

    def mark_as_read(self, user):
        """Mark message as read by user"""
        if user not in self.read_by.all():
            self.read_by.add(user)

    def get_read_count(self):
        """Get number of users who read this message"""
        return self.read_by.count()

    def soft_delete(self):
        """Soft delete message"""
        self.is_deleted = True
        self.content = "[This message has been deleted]"
        self.save()

    def format_for_websocket(self):
        """Format message for WebSocket transmission"""
        return {
            'id': str(self.message_id),
            'type': self.message_type,
            'content': self.content if not self.is_deleted else "[Deleted]",
            'user': {
                'id': self.user.id if self.user else None,
                'username': self.user.username if self.user else 'System',
                'avatar': getattr(self.user, 'profile', None).avatar.url if self.user and hasattr(self.user,
                                                                                                  'profile') and self.user.profile.avatar else None,
            },
            'timestamp': self.created_at.isoformat(),
            'is_edited': self.is_edited,
            'is_deleted': self.is_deleted,
            'reply_to_id': str(self.reply_to.message_id) if self.reply_to else None,
            'mentions': [{'id': u.id, 'username': u.username} for u in self.mentions.all()],
            'read_count': self.get_read_count(),
            'attachment': {
                'has_image': bool(self.image),
                'image_url': self.image.url if self.image else None,
                'has_file': bool(self.file),
                'file_name': self.file_name,
                'file_size': self.file_size,
            } if self.image or self.file else None,
        }

    def __str__(self):
        return f"{self.message_id} - {self.user.username if self.user else 'System'}: {self.content[:50]}"


class ChatActivity(TimeStampedModel):
    """
    Track user activities in chat rooms (for logging/moderation).
    """
    ACTIVITY_TYPES = [
        ('JOINED', 'Joined Room'),
        ('LEFT', 'Left Room'),
        ('BAN', 'User Banned'),
        ('UNBAN', 'User Unbanned'),
        ('MOD_ADD', 'Moderator Added'),
        ('MOD_REMOVE', 'Moderator Removed'),
        ('KICK', 'User Kicked'),
        ('MUTE', 'User Muted'),
        ('UNMUTE', 'User Unmuted'),
        ('PIN', 'Message Pinned'),
        ('UNPIN', 'Message Unpinned'),
        ('CLEAR', 'Chat Cleared'),
    ]

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='activities')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_activities')
    action = models.CharField(max_length=20, choices=ACTIVITY_TYPES, db_index=True)

    target_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='targeted_activities')
    message = models.ForeignKey(ChatMessage, on_delete=models.SET_NULL, null=True, blank=True)

    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['room', '-created_at']),
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['action', '-created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} {self.action} in {self.room.name}"


class ChatNotification(models.Model):
    """
    User notification preferences for chat.
    """
    NOTIFICATION_TYPES = [
        ('ALL', 'All Messages'),
        ('MENTIONS', 'Only Mentions'),
        ('NONE', 'No Notifications'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='chat_notification_settings')

    # Global settings
    sound_enabled = models.BooleanField(default=True)
    desktop_notifications = models.BooleanField(default=True)
    email_notifications = models.BooleanField(default=False)

    # Room-specific settings (JSON field)
    room_settings = models.JSONField(default=dict, blank=True,
                                     help_text="Format: {'room_id': {'mute': bool, 'notification_type': 'ALL/MENTIONS/NONE'}}")

    # Do not disturb
    dnd_enabled = models.BooleanField(default=False)
    dnd_start = models.TimeField(null=True, blank=True)
    dnd_end = models.TimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Chat Notification Setting"
        verbose_name_plural = "Chat Notification Settings"

    def should_notify(self, room, message):
        """Check if user should be notified about message"""
        if self.dnd_enabled and self.dnd_start and self.dnd_end:
            now = timezone.now().time()
            if self.dnd_start <= now <= self.dnd_end:
                return False

        room_setting = self.room_settings.get(str(room.id), {})
        if room_setting.get('mute', False):
            return False

        notify_type = room_setting.get('notification_type', 'ALL')

        if notify_type == 'NONE':
            return False
        elif notify_type == 'MENTIONS':
            return self.user in message.mentions.all()

        return True

    def __str__(self):
        return f"{self.user.username}'s chat settings"


class ChatReaction(models.Model):
    """
    Message reactions (like emoji responses).
    """
    REACTION_TYPES = [
        ('👍', 'Thumbs Up'),
        ('❤️', 'Heart'),
        ('😂', 'Laughing'),
        ('😮', 'Wow'),
        ('😢', 'Sad'),
        ('👏', 'Applause'),
        ('🎉', 'Celebration'),
        ('🔥', 'Fire'),
        ('💯', '100'),
        ('❓', 'Question'),
    ]

    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_reactions')
    reaction = models.CharField(max_length=10, choices=REACTION_TYPES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['message', 'user', 'reaction']
        indexes = [
            models.Index(fields=['message', 'reaction']),
        ]

    def __str__(self):
        return f"{self.user.username} reacted {self.reaction} to message"


class DirectMessage(TimeStampedModel):
    """
    Direct messaging between two users.
    """
    dm_id = models.CharField(max_length=50, unique=True, default=uuid.uuid4, editable=False)
    user1 = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dm_initiated')
    user2 = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dm_received')

    # Last message preview
    last_message = models.TextField(blank=True, max_length=200)
    last_message_time = models.DateTimeField(auto_now=True)

    # Unread counts
    user1_unread = models.IntegerField(default=0)
    user2_unread = models.IntegerField(default=0)

    # Status
    user1_blocked = models.BooleanField(default=False)
    user2_blocked = models.BooleanField(default=False)
    user1_muted = models.BooleanField(default=False)
    user2_muted = models.BooleanField(default=False)

    class Meta:
        unique_together = ['user1', 'user2']
        ordering = ['-last_message_time']
        indexes = [
            models.Index(fields=['user1', '-last_message_time']),
            models.Index(fields=['user2', '-last_message_time']),
        ]

    def get_other_user(self, user):
        """Get the other participant"""
        return self.user2 if user == self.user1 else self.user1

    def increment_unread(self, sender):
        """Increment unread count for recipient"""
        if sender == self.user1:
            self.user2_unread += 1
        else:
            self.user1_unread += 1
        self.save()

    def mark_as_read(self, user):
        """Mark all messages as read for user"""
        if user == self.user1:
            self.user1_unread = 0
        else:
            self.user2_unread = 0
        self.save()

    def __str__(self):
        return f"DM: {self.user1.username} - {self.user2.username}"


class DirectMessageContent(TimeStampedModel):
    """
    Individual direct messages.
    """
    dm = models.ForeignKey(DirectMessage, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_dms')
    content = models.TextField()

    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    is_delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['dm', '-created_at']),
            models.Index(fields=['sender', '-created_at']),
            models.Index(fields=['is_read', 'dm']),
        ]

    def mark_as_read(self):
        """Mark message as read"""
        self.is_read = True
        self.read_at = timezone.now()
        self.save()

    def mark_as_delivered(self):
        """Mark message as delivered"""
        self.is_delivered = True
        self.delivered_at = timezone.now()
        self.save()

    def __str__(self):
        return f"{self.sender.username}: {self.content[:50]}"


class ChatNotificationMessage(models.Model):
    """
    Actual notification messages sent to users
    """
    NOTIFICATION_TYPES = [
        ('INVITE', 'Room Invite'),
        ('JOIN', 'User Joined'),
        ('LEAVE', 'User Left'),
        ('BAN', 'User Banned'),
        ('UNBAN', 'User Unbanned'),
        ('MOD_ADD', 'Moderator Added'),
        ('MOD_REMOVE', 'Moderator Removed'),
        ('EVENT', 'Room Event'),
        ('ANNOUNCEMENT', 'Announcement'),
        ('ROOM_DELETED', 'Room Deleted'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_notification_messages')
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, db_index=True)
    message = models.CharField(max_length=255)
    related_object_id = models.IntegerField(null=True, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'read', '-created_at']),
            models.Index(fields=['notification_type', '-created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.notification_type} - {self.created_at}"


# ==================== CHAT SIGNALS ====================

@receiver(post_save, sender=User)
def create_chat_notification_settings(sender, instance, created, **kwargs):
    """Create chat notification settings for new users"""
    if created:
        ChatNotification.objects.get_or_create(user=instance)


@receiver(post_save, sender=ChatMessage)
def create_mention_notifications(sender, instance, created, **kwargs):
    """Create notifications for mentions"""
    if created and instance.mentions.exists():
        pass


@receiver(post_save, sender=ChatRoom)
def create_room_activity(sender, instance, created, **kwargs):
    """Log room creation"""
    if created and instance.created_by:
        ChatActivity.objects.create(
            room=instance,
            user=instance.created_by,
            action='JOINED'
        )


class ChatDashboard:
    """Helper class for chat dashboard statistics"""

    @staticmethod
    def get_stats():
        """Get chat system statistics"""
        from django.db.models import Count, Avg

        today = timezone.now().date()
        week_ago = today - timezone.timedelta(days=7)

        return {
            'rooms': {
                'total': ChatRoom.objects.count(),
                'active_today': ChatRoom.objects.filter(last_activity__date=today).count(),
                'public': ChatRoom.objects.filter(room_type='PUBLIC').count(),
                'private': ChatRoom.objects.filter(room_type='PRIVATE').count(),
                'direct': ChatRoom.objects.filter(room_type='DIRECT').count(),
            },
            'messages': {
                'total': ChatMessage.objects.count(),
                'today': ChatMessage.objects.filter(created_at__date=today).count(),
                'this_week': ChatMessage.objects.filter(created_at__date__gte=week_ago).count(),
                'avg_per_room': ChatMessage.objects.values('room').annotate(
                    count=Count('id')
                ).aggregate(avg=models.Avg('count'))['avg'] or 0,
            },
            'users': {
                'active_chatters': User.objects.filter(
                    chat_messages__created_at__date=today
                ).distinct().count(),
                'users_in_chat': User.objects.filter(
                    chat_rooms__isnull=False
                ).distinct().count(),
                'online_now': User.objects.filter(
                    profile__online_status=True
                ).count(),
            },
            'activity': {
                'joins': ChatActivity.objects.filter(action='JOINED', created_at__date=today).count(),
                'leaves': ChatActivity.objects.filter(action='LEFT', created_at__date=today).count(),
                'reports': 0,
            }
        }
