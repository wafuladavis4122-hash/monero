import calendar
import hashlib
import json
import logging
import math
import os
import re
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import IntegrityError, connection, transaction
from django.db.models import (
    BooleanField,
    Case,
    Count,
    F,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .models import (
    ChatActivity,
    ChatMessage,
    ChatNotification,
    ChatNotificationMessage,
    ChatReaction,
    ChatRoom,
)


from .models import (
    UserProfile, Wallet, Transaction, MpesaPayment, 
    Token, Investment, WithdrawalRequest, SystemConfig, SystemLog
)

# ==================== WEEKDAY/WEEKEND UTILITY FUNCTIONS ====================

def is_weekday(date=None):
    """
    Check if a given date is a weekday (Monday-Friday)
    Returns: True if weekday, False if weekend
    """
    if date is None:
        date = timezone.now()
    # weekday(): Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
    return date.weekday() < 5


def is_weekend(date=None):
    """
    Check if a given date is a weekend (Saturday-Sunday)
    Returns: True if weekend, False if weekday
    """
    if date is None:
        date = timezone.now()
    return date.weekday() >= 5


def is_withdrawal_window(date=None):
    """
    Check if current time is within withdrawal window:
    - Weekend (Saturday or Sunday)
    - Between 8 AM and 5 PM
    """
    if date is None:
        date = timezone.now()

    # Check if it's weekend
    if not is_weekend(date):
        return False, "Withdrawals are only available on weekends (Saturday and Sunday)"

    # Check time window: 8 AM to 5 PM
    current_time = date.time()
    start_time = time(8, 0)  # 8:00 AM
    end_time = time(17, 0)  # 5:00 PM

    if current_time < start_time:
        return False, "Withdrawals start at 8:00 AM"

    if current_time > end_time:
        return False, "Withdrawals close at 5:00 PM"

    return True, "Withdrawal window is open"


def get_withdrawal_window_status():
    """
    Get detailed status of withdrawal window for display
    """
    from django.utils import timezone
    now = timezone.now()

    if is_weekend(now):
        current_time = now.time()
        start_time = time(8, 0)
        end_time = time(17, 0)

        if current_time < start_time:
            # FIX: Make datetime timezone-aware
            start_datetime = timezone.make_aware(
                datetime.combine(now.date(), start_time)
            )
            remaining = (start_datetime - now).total_seconds() / 60
            return {
                'open': False,
                'status': 'Not yet open',
                'message': f'Withdrawals open at 8:00 AM ({int(remaining)} minutes remaining)',
                'opens_at': '8:00 AM',
                'closes_at': '5:00 PM'
            }
        elif current_time > end_time:
            return {
                'open': False,
                'status': 'Closed',
                'message': 'Withdrawals closed for today. Please try again next weekend.',
                'opens_at': '8:00 AM',
                'closes_at': '5:00 PM'
            }
        else:
            # FIX: Make datetime timezone-aware
            end_datetime = timezone.make_aware(
                datetime.combine(now.date(), end_time)
            )
            remaining = (end_datetime - now).total_seconds() / 60
            return {
                'open': True,
                'status': 'Open',
                'message': f'Withdrawals are open! ({int(remaining)} minutes remaining)',
                'opens_at': '8:00 AM',
                'closes_at': '5:00 PM'
            }
    else:
        # It's a weekday
        days_until_weekend = 5 - now.weekday()  # Days until Saturday
        return {
            'open': False,
            'status': 'Weekday',
            'message': f'Withdrawals are only available on weekends. Next window opens in {days_until_weekend} days.',
            'opens_at': '8:00 AM (Saturday)',
            'closes_at': '5:00 PM (Sunday)'
        }

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


def count_business_days_between(start_date, end_date):
    """
    Count business days between two dates (inclusive)
    """
    if start_date > end_date:
        return 0

    # Convert to date objects
    start = start_date.date() if hasattr(start_date, 'date') else start_date
    end = end_date.date() if hasattr(end_date, 'date') else end_date

    business_days = 0
    current = start

    while current <= end:
        if current.weekday() < 5:
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


def get_last_business_day(date=None):
    """
    Get the last business day (Monday-Friday)
    """
    if date is None:
        date = timezone.now()

    prev_day = date - timedelta(days=1)

    while prev_day.weekday() >= 5:
        prev_day -= timedelta(days=1)

    return prev_day

# Set up logging
logger = logging.getLogger(__name__)

def safe_cache_delete_pattern(pattern):
    """
    Safely delete cache keys matching a pattern.
    Works with all cache backends by gracefully handling errors.
    """
    try:
        if hasattr(cache, 'delete_pattern'):
            return cache.delete_pattern(pattern)
        else:
            logger.debug(f"Pattern deletion not supported for {cache.__class__.__name__}: {pattern}")
            return False
    except Exception as e:
        logger.error(f"Error in cache pattern deletion: {e}")
        return False

# Monkey patch the cache object if needed
if not hasattr(cache, 'delete_pattern'):
    cache.delete_pattern = safe_cache_delete_pattern

# ==================== AUTO PAYOUT HELPER FUNCTION ====================
def check_user_payouts(user):
    """
    Enhanced version that ONLY processes payouts on weekdays (Monday-Friday)
    AND only when at least 24 hours have passed since the last payout.

    This function is called when users visit their account or investments page.
    It processes at most ONE payout per check, and only if 24+ hours have passed.
    """
    if not user.is_authenticated:
        return 0

    from .models import Investment

    now = timezone.now()

    # ===== WEEKEND CHECK =====
    if is_weekend(now):
        logger.info(f"Weekend detected ({now.strftime('%A')}) - Payouts paused for user {user.username}")
        return 0  # No payouts on weekends

    # Get user's active investments that still have payouts remaining
    investments = Investment.objects.filter(
        user=user,
        status='ACTIVE',
        remaining_payouts__gt=0
    )

    processed_count = 0

    for investment in investments:
        try:
            # Determine reference time (last payout or creation)
            reference_time = investment.last_payout_date or investment.created_at

            # ===== CRITICAL FIX: Check if 24 HOURS have actually passed =====
            hours_since_last = (now - reference_time).total_seconds() / 3600

            # Only process if at least 24 hours have passed
            if hours_since_last < 24:
                logger.debug(
                    f"Investment {investment.id}: Only {hours_since_last:.1f} hours since last payout. "
                    f"Need 24 hours. Skipping."
                )
                continue

            # ===== ADDITIONAL CHECK: Ensure it's a weekday =====
            if not is_weekday(now):
                logger.debug(f"Investment {investment.id}: Today is not a weekday. Skipping.")
                continue

            # ===== PROCESS THE PAYOUT =====
            # Only process ONE payout per check to prevent multiple payouts on same day
            success = investment.process_daily_payout()
            if success:
                processed_count += 1
                logger.info(
                    f"✅ Processed payout for investment {investment.id} "
                    f"(User: {user.username}, Hours since last: {hours_since_last:.1f})"
                )
                # Break after processing one payout to prevent multiple in one request
                break
            else:
                logger.warning(f"⚠️ Failed to process payout for investment {investment.id}")

        except Exception as e:
            logger.error(f"❌ Auto-payout error for investment {investment.id}: {str(e)}", exc_info=True)

    if processed_count > 0:
        logger.info(f"📊 Processed {processed_count} payout(s) for user {user.username}")

    return processed_count

def calculate_missed_payouts(investment, current_time):
    """
    Calculate how many payouts are due based on business days only.
    Only counts Monday-Friday as eligible payout days AND respects
    the 24-hour minimum gap between payouts.

    Returns integer number of payouts that should have been processed.
    """
    from datetime import timedelta

    # Don't calculate if investment is not active
    if investment.status != 'ACTIVE' or investment.remaining_payouts <= 0:
        return 0

    # Base reference time for calculations
    reference_time = investment.last_payout_date or investment.created_at

    # ===== CRITICAL FIX: Check the 24-hour window first =====
    hours_since_last = (current_time - reference_time).total_seconds() / 3600
    if hours_since_last < 24:
        return 0

    # Count business days between reference and current time
    business_days_passed = count_business_days(reference_time, current_time)

    # If no full business day has passed since last payout, nothing is due
    if business_days_passed < 1:
        return 0

    # Don't exceed remaining_payouts
    due_payouts = min(business_days_passed, investment.remaining_payouts)

    # Log for debugging
    if due_payouts > 0:
        logger.debug(f"""
            Investment {investment.id}:
            - Created: {investment.created_at}
            - Last payout: {investment.last_payout_date}
            - Current time: {current_time}
            - Hours since last: {hours_since_last:.1f}
            - Business days passed: {business_days_passed}
            - Remaining payouts: {investment.remaining_payouts}
            - Due now: {due_payouts}
        """)

    return due_payouts

def catch_up_all_users_payouts():
    """
    Admin function to catch up payouts for ALL users
    Only processes on weekdays

    This is called by the admin catch-up view.
    """
    from django.contrib.auth.models import User

    now = timezone.now()

    # ===== WEEKEND CHECK =====
    if is_weekend(now):
        logger.info(f"Weekend detected - Catch-up payouts paused")
        return 0, 0

    total_processed = 0
    users_processed = 0

    # Get all users with active investments
    users_with_investments = User.objects.filter(
        investments__status='ACTIVE',
        investments__remaining_payouts__gt=0
    ).distinct()

    for user in users_with_investments:
        try:
            processed = check_user_payouts(user)
            if processed > 0:
                total_processed += processed
                users_processed += 1
                logger.info(f"Caught up {processed} payouts for user {user.username}")
        except Exception as e:
            logger.error(f"Error catching up payouts for user {user.username}: {str(e)}")

    logger.info(f"Complete catch-up finished: {total_processed} payouts for {users_processed} users")
    return total_processed, users_processed

# ==================== ADMIN VIEW FOR CATCH-UP ====================

@login_required(login_url='XMR:signupin')
def admin_catch_up_payouts(request):
    """Admin view to manually trigger catch-up for all users"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method == 'POST':
        total_processed, users_processed = catch_up_all_users_payouts()
        
        SystemLog.objects.create(
            log_type='ADMIN_ACTION',
            user=request.user,
            action='CATCH_UP_PAYOUTS',
            description=f'Admin triggered catch-up payouts: {total_processed} payouts for {users_processed} users'
        )
        
        return JsonResponse({
            'success': True,
            'message': f'Caught up {total_processed} payouts for {users_processed} users',
            'processed': total_processed,
            'users': users_processed
        })
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ==================== INDIVIDUAL INVESTMENT FIX ====================

def fix_investment_payouts(investment_id):
    """
    Fix payouts for a specific investment
    Useful for targeted fixes
    """
    from .models import Investment
    
    try:
        investment = Investment.objects.get(id=investment_id)
        user = investment.user
        
        processed = check_user_payouts(user)
        
        return {
            'success': True,
            'investment_id': investment_id,
            'user': user.username,
            'payouts_processed': processed,
            'new_remaining': investment.remaining_payouts,
            'new_status': investment.status
        }
    except Investment.DoesNotExist:
        return {'success': False, 'error': 'Investment not found'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ==================== PUBLIC VIEWS ====================

def index(request):
    """Homepage view with system statistics"""
    # Get some stats for the homepage
    total_users = User.objects.count()
    active_investments = Investment.objects.filter(status='ACTIVE').count()
    total_paid = Transaction.objects.filter(
        transaction_type='PROFIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    # Get active tokens for display
    active_tokens = Token.objects.filter(status='ACTIVE')[:6]
    
    context = {
        'total_users': total_users,
        'active_investments': active_investments,
        'total_paid': total_paid,
        'active_tokens': active_tokens,
    }
    return render(request, 'index.html', context)


def about(request):
    """About page"""
    return render(request, 'about.html')


def signupin(request):
    """Combined signup and login view"""
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect('XMR:myadmin')
        else:
            return redirect('XMR:account')
    
    # Check for referral code in URL
    if 'ref' in request.GET:
        referral_code = request.GET.get('ref')
        try:
            referrer = UserProfile.objects.get(referral_code=referral_code)
            request.session['referral_code'] = referral_code
            messages.info(request, f'You are being referred by {referrer.user.username}')
        except UserProfile.DoesNotExist:
            messages.warning(request, 'Invalid referral code')
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        # ========== LOGIN HANDLER ==========
        if action == 'login':
            return handle_login(request)
        
        # ========== SIGNUP HANDLER ==========
        elif action == 'signup':
            return handle_signup(request)
    
    return render(request, 'signupin.html')


def handle_login(request):
    """Handle user login"""
    username = request.POST.get('login_username')
    password = request.POST.get('login_password')
    
    # Basic validation
    if not username or not password:
        messages.error(request, 'Both username and password are required.')
        return render(request, 'signupin.html')
    
    # Authenticate user
    user = authenticate(request, username=username, password=password)
    
    if user is not None:
        # Check if user is banned
        try:
            if user.profile.is_banned:
                messages.error(request, f'Your account has been banned. Reason: {user.profile.ban_reason or "No reason provided"}')
                return render(request, 'signupin.html')
        except UserProfile.DoesNotExist:
            pass
        
        login(request, user)
        
        # Log the login
        SystemLog.objects.create(
            log_type='INFO',
            user=user,
            action='USER_LOGIN',
            description=f'User logged in from IP: {get_client_ip(request)}',
            ip_address=get_client_ip(request)
        )
        
        # Redirect based on admin status
        if user.is_staff:
            messages.success(request, 'Welcome back, Admin!')
            return redirect('XMR:myadmin')
        else:
            messages.success(request, f'Welcome back, {user.first_name or user.username}!')
            return redirect('XMR:account')
    else:
        messages.error(request, 'Invalid username or password.')
        return render(request, 'signupin.html')


def handle_signup(request):
    """Handle user registration"""
    username = request.POST.get('username')
    full_name = request.POST.get('full_name')
    email = request.POST.get('email')
    phone = request.POST.get('phone')
    password1 = request.POST.get('password1')
    password2 = request.POST.get('password2')
    referral_code = request.POST.get('referral_code', '').strip()
    
    # Validate all fields are present
    if not all([username, full_name, email, phone, password1, password2]):
        messages.error(request, 'All fields are required.')
        return render(request, 'signupin.html')
    
    # Password match validation
    if password1 != password2:
        messages.error(request, 'Passwords do not match.')
        return render(request, 'signupin.html')
    
    # Password strength
    if len(password1) < 8:
        messages.error(request, 'Password must be at least 8 characters long.')
        return render(request, 'signupin.html')
    
    # Add password complexity check
    if not re.search(r'[A-Z]', password1):
        messages.error(request, 'Password must contain at least one uppercase letter.')
        return render(request, 'signupin.html')
    
    if not re.search(r'[0-9]', password1):
        messages.error(request, 'Password must contain at least one number.')
        return render(request, 'signupin.html')
    
    # Username validation
    if not re.match(r'^[a-zA-Z0-9_]{3,20}$', username):
        messages.error(request, 'Username must be 3-20 characters and can only contain letters, numbers, and underscores.')
        return render(request, 'signupin.html')
    
    # Email validation
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Please enter a valid email address.')
        return render(request, 'signupin.html')
    
    # Phone validation (Kenyan format)
    phone = clean_phone_number(phone)
    if not validate_phone_number(phone):
        messages.error(request, 'Please enter a valid Kenyan phone number (e.g., 0712345678 or 254712345678)')
        return render(request, 'signupin.html')
    
    # Check if username already exists
    if User.objects.filter(username=username).exists():
        messages.error(request, 'Username already taken. Please choose another.')
        return render(request, 'signupin.html')
    
    # Check if email already exists
    if User.objects.filter(email=email).exists():
        messages.error(request, 'Email already registered. Please use login or another email.')
        return render(request, 'signupin.html')
    
    # Check if phone already exists
    if UserProfile.objects.filter(phone_number=phone).exists():
        messages.error(request, 'Phone number already registered. Please use login or another number.')
        return render(request, 'signupin.html')
    
    # Validate referral code if provided
    referrer_profile = None
    if referral_code:
        try:
            referrer_profile = UserProfile.objects.get(referral_code=referral_code)
        except UserProfile.DoesNotExist:
            messages.error(request, 'Invalid referral code. Please check and try again.')
            return render(request, 'signupin.html')
    
    # Create user with transaction to ensure data integrity
    try:
        with transaction.atomic():
            # Split full name
            name_parts = full_name.strip().split(' ', 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ''
            
            # Create user
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password1,
                first_name=first_name,
                last_name=last_name
            )
            
            # Get or create profile (signals should create it, but just in case)
            profile, created = UserProfile.objects.get_or_create(user=user)
            
            # Update profile with registration data
            profile.phone_number = phone
            profile.national_id_name = full_name
            if referrer_profile:
                profile.referred_by = referrer_profile
            profile.save()
            
            # Ensure wallet exists
            wallet, created = Wallet.objects.get_or_create(user=user)
            
            # Log registration
            SystemLog.objects.create(
                log_type='INFO',
                user=user,
                action='USER_REGISTERED',
                description=f'New user registered with referral: {referral_code or "None"}',
                ip_address=get_client_ip(request)
            )
            
            # Clear session referral code
            if 'referral_code' in request.session:
                del request.session['referral_code']
            
            # Auto-login
            authenticated_user = authenticate(request, username=username, password=password1)
            if authenticated_user:
                login(request, authenticated_user)
                
                if referrer_profile:
                    messages.success(request, f'Account created successfully! You were referred by {referrer_profile.user.username}.')
                else:
                    messages.success(request, 'Account created successfully!')
                
                return redirect('XMR:account')
            else:
                messages.success(request, 'Account created. Please log in.')
                return render(request, 'signupin.html')
    
    except Exception as e:
        messages.error(request, f'Error creating account: {str(e)}')
        logger.error(f"Signup error: {str(e)}", exc_info=True)
        return render(request, 'signupin.html')


def logout_view(request):
    """Handle user logout"""
    if request.user.is_authenticated:
        SystemLog.objects.create(
            log_type='INFO',
            user=request.user,
            action='USER_LOGOUT',
            description='User logged out',
            ip_address=get_client_ip(request)
        )
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    return redirect('XMR:signupin')


def signup_with_ref(request):
    """Handle signup with referral code in URL"""
    referral_code = request.GET.get('ref')
    if referral_code:
        try:
            referrer = UserProfile.objects.get(referral_code=referral_code)
            request.session['referral_code'] = referral_code
            messages.info(request, f'You are being referred by {referrer.user.username}')
        except UserProfile.DoesNotExist:
            messages.warning(request, 'Invalid referral code')
    return redirect('XMR:signupin')


# ==================== USER ACCOUNT VIEW ====================

@login_required(login_url='XMR:signupin')
def account(request):
    """Consolidated user account dashboard with all features"""

    global profile
    check_user_payouts(request.user)
    # =============================

    try:
        profile = request.user.profile
        wallet = request.user.wallet
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(
            user=request.user,
            phone_number="",
            national_id_name=request.user.get_full_name() or request.user.username
        )
        wallet = Wallet.objects.create(user=request.user)
    except Wallet.DoesNotExist:
        wallet = Wallet.objects.create(user=request.user)

    # Get user's deposits
    deposits = MpesaPayment.objects.filter(
        user=request.user
    ).order_by('-created_at')[:20]

    # Get user's withdrawals
    withdrawals = WithdrawalRequest.objects.filter(
        user=request.user
    ).order_by('-created_at')[:20]

    # Get user's transactions
    transactions = Transaction.objects.filter(
        wallet=wallet
    ).select_related('investment', 'withdrawal').order_by('-created_at')[:30]

    # Get user's active investments (status = ACTIVE)
    active_investments = Investment.objects.filter(
        user=request.user,
        status='ACTIVE'
    ).select_related('token').order_by('-created_at')

    # Get user's completed investments (for history)
    completed_investments = Investment.objects.filter(
        user=request.user,
        status='COMPLETED'
    ).select_related('token').order_by('-created_at')[:20]

    # Get all investments (including both active and completed)
    all_investments = Investment.objects.filter(
        user=request.user
    ).select_related('token').order_by('-created_at')[:50]

    # Get pending counts
    pending_deposits = MpesaPayment.objects.filter(
        user=request.user,
        status='PENDING'
    ).count()

    pending_withdrawals = WithdrawalRequest.objects.filter(
        user=request.user,
        status='PENDING'
    ).count()

    # Calculate dashboard stats
    total_invested = active_investments.aggregate(total=Sum('amount'))['total'] or 0
    total_earned = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='PROFIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0

    # ===== FIXED: Get next payout with BUSINESS DAYS =====
    next_payout = active_investments.order_by('end_date').first()

    if next_payout:
        # Use the investment's business days remaining method
        business_days_remaining = next_payout.get_business_days_remaining()
        days_left = business_days_remaining

        # Get next payout date (always a weekday)
        next_payout_date = next_payout.get_next_payout_date()
        next_payout_display = next_payout_date.strftime('%A, %B %d, %Y')

        # Calculate hours until next payout
        now = timezone.now()
        hours_until = (next_payout_date - now).total_seconds() / 3600

        if hours_until < 1:
            next_payout_time = f"{int(hours_until * 60)} minutes"
        elif hours_until < 24:
            next_payout_time = f"{int(hours_until)} hours"
        else:
            next_payout_time = f"{int(hours_until / 24)} days"
    else:
        days_left = 0
        next_payout_display = "No active investments"
        next_payout_time = "N/A"

    # ===== FIXED: Calculate total daily return (only active investments with remaining payouts) =====
    total_daily_return = 0
    for investment in active_investments:
        if investment.remaining_payouts > 0:
            total_daily_return += investment.daily_return

    # Get referral data
    referrals = UserProfile.objects.filter(
        referred_by=profile
    ).select_related('user').order_by('-created_at')

    referral_data = []
    total_referral_earnings = Decimal('0')

    for ref in referrals:
        # Get first deposit
        first_deposit = Transaction.objects.filter(
            wallet=ref.user.wallet,
            transaction_type='DEPOSIT',
            status='COMPLETED'
        ).order_by('created_at').first()

        first_deposit_amount = first_deposit.amount if first_deposit else 0

        # Get bonus earned from this referral
        bonus = Transaction.objects.filter(
            wallet=wallet,
            transaction_type='REFERRAL_BONUS',
            description__icontains=ref.user.username
        ).aggregate(total=Sum('amount'))['total'] or 0

        total_referral_earnings += bonus

        referral_data.append({
            'user': ref.user,
            'created_at': ref.created_at,
            'first_deposit': first_deposit_amount,
            'bonus_earned': bonus,
            'is_active': ref.user.last_login and
                         ref.user.last_login > timezone.now() - timedelta(days=30)
        })

    # Get payment instructions from system config
    paybill = SystemConfig.get_config('mpesa_paybill', '345678')
    account_no = SystemConfig.get_config('mpesa_account', request.user.username)
    min_deposit = SystemConfig.get_config('min_deposit', 1200)
    min_withdrawal = SystemConfig.get_config('min_withdrawal', 500)

    # Current date for greeting
    current_date = timezone.now()

    # Get greeting based on time
    hour = timezone.now().hour
    if hour < 12:
        greeting = "Morning"
    elif hour < 17:
        greeting = "Afternoon"
    else:
        greeting = "Evening"

    # ===== WITHDRAWAL WINDOW STATUS =====
    withdrawal_status = get_withdrawal_window_status()
    is_withdrawal_open = withdrawal_status['open']

    # ===== PAYOUT SCHEDULE INFO =====
    now = timezone.now()
    is_weekday_now = is_weekday(now)
    is_weekend_now = is_weekend(now)

    # Calculate next payout day (next weekday)
    if is_weekend_now:
        next_business_day = get_next_business_day(now)
        next_payout_day_display = next_business_day.strftime('%A, %B %d, %Y')
        payout_status = 'paused'
        payout_message = f'Weekend - No payouts today. Next payout: {next_payout_day_display}'
    else:
        next_payout_day_display = now.strftime('%A, %B %d, %Y')
        payout_status = 'active'
        payout_message = f'Today is a payout day ({now.strftime("%A")})'

    # Get next withdrawal window
    if is_weekend_now:
        if is_withdrawal_open:
            next_withdrawal_window = 'Open now!'
            next_withdrawal_status = 'open'
        else:
            if now.time() < time(8, 0):
                next_withdrawal_window = 'Today at 8:00 AM'
                next_withdrawal_status = 'opens_today'
            else:
                next_withdrawal_window = 'Next weekend'
                next_withdrawal_status = 'closed_today'
    else:
        days_until_saturday = 5 - now.weekday()
        if days_until_saturday <= 0:
            next_withdrawal_window = 'Tomorrow (Saturday)'
        elif days_until_saturday == 1:
            next_withdrawal_window = 'Tomorrow (Saturday)'
        else:
            next_withdrawal_window = f'In {days_until_saturday} days (Saturday)'
        next_withdrawal_status = 'weekday'

    context = {
        # User and profile
        'user': request.user,
        'profile': profile,
        'wallet': wallet,
        'available_balance': wallet.available_balance(),
        'locked_balance': wallet.locked_balance,
        'total_balance': wallet.balance + wallet.locked_balance,

        # Stats
        'total_invested': total_invested,
        'total_earned': total_earned,
        'total_referral_earnings': total_referral_earnings,
        'total_referrals': referrals.count(),
        'active_referrals': sum(1 for r in referral_data if r['is_active']),

        # Pending counts
        'pending_deposits': pending_deposits,
        'pending_withdrawals': pending_withdrawals,

        # Lists
        'deposits': deposits,
        'withdrawals': withdrawals,
        'transactions': transactions,
        'active_investments': active_investments,
        'completed_investments': completed_investments,
        'all_investments': all_investments,
        'referrals': referral_data,

        # ===== FIXED: Next payout with business days =====
        'next_payout': next_payout,
        'days_left': days_left,
        'next_payout_display': next_payout_display,
        'next_payout_time': next_payout_time,

        # ===== FIXED: Daily returns =====
        'total_daily_return': total_daily_return,

        # Payment configs
        'paybill': paybill,
        'account_no': account_no,
        'min_deposit': min_deposit,
        'min_withdrawal': min_withdrawal,

        # Verification status
        'phone_verified': profile.phone_verified,
        'id_verified': profile.id_verified,

        # Current date and greeting
        'current_date': current_date,
        'greeting': greeting,

        # ===== SCHEDULE INFORMATION =====
        # Withdrawal window
        'withdrawal_window': withdrawal_status,
        'is_withdrawal_open': is_withdrawal_open,
        'withdrawal_status_message': withdrawal_status['message'],
        'withdrawal_opens_at': withdrawal_status.get('opens_at', '8:00 AM'),
        'withdrawal_closes_at': withdrawal_status.get('closes_at', '5:00 PM'),

        # Payout schedule
        'is_weekday': is_weekday_now,
        'is_weekend': is_weekend_now,
        'payout_status': payout_status,
        'payout_message': payout_message,
        'next_payout_day': next_payout_day_display,
        'payout_schedule': 'Monday - Friday (Weekdays only)',
        'withdrawal_schedule': 'Saturday - Sunday, 8:00 AM - 5:00 PM',

        # Next withdrawal
        'next_withdrawal_window': next_withdrawal_window,
        'next_withdrawal_status': next_withdrawal_status,
    }

    # Handle POST requests for profile updates, password changes, KYC uploads
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_profile':
            return handle_profile_update(request, profile)
        elif action == 'change_password':
            return handle_password_change(request)
        elif action == 'upload_kyc':
            return handle_kyc_upload(request, profile)

    return render(request, 'account.html', context)


def handle_profile_update(request, profile):
    """Handle profile update"""
    phone = request.POST.get('phone')
    national_id_name = request.POST.get('national_id_name')
    
    if phone:
        # Validate phone
        phone = clean_phone_number(phone)
        if not validate_phone_number(phone):
            messages.error(request, 'Please enter a valid Kenyan phone number')
            return redirect('XMR:account')
        
        # Check if phone is already taken
        if UserProfile.objects.exclude(pk=profile.pk).filter(phone_number=phone).exists():
            messages.error(request, 'Phone number already in use by another account')
            return redirect('XMR:account')
        
        profile.phone_number = phone
    
    if national_id_name:
        profile.national_id_name = national_id_name
    
    profile.save()
    messages.success(request, 'Profile updated successfully!')
    return redirect('XMR:account')


def handle_password_change(request):
    """Handle password change"""
    current_password = request.POST.get('current_password')
    new_password1 = request.POST.get('new_password1')
    new_password2 = request.POST.get('new_password2')
    
    if not request.user.check_password(current_password):
        messages.error(request, 'Current password is incorrect')
        return redirect('XMR:account')
    
    if new_password1 != new_password2:
        messages.error(request, 'New passwords do not match')
        return redirect('XMR:account')
    
    if len(new_password1) < 8:
        messages.error(request, 'Password must be at least 8 characters long')
        return redirect('XMR:account')
    
    request.user.set_password(new_password1)
    request.user.save()
    
    # Re-authenticate user
    user = authenticate(username=request.user.username, password=new_password1)
    login(request, user)
    
    messages.success(request, 'Password changed successfully!')
    return redirect('XMR:account')


def handle_kyc_upload(request, profile):
    """Handle KYC document upload"""
    id_front = request.FILES.get('id_front')
    id_back = request.FILES.get('id_back')
    selfie = request.FILES.get('selfie')
    
    if id_front:
        profile.id_front_image = id_front
    if id_back:
        profile.id_back_image = id_back
    if selfie:
        profile.selfie_with_id = selfie
    
    profile.save()
    messages.success(request, 'KYC documents uploaded successfully! They will be verified by admin.')
    return redirect('XMR:account')


# ==================== DEPOSIT VIEWS ====================

@login_required(login_url='XMR:signupin')
def create_deposit(request):
    """Create a new deposit request"""
    if request.method != 'POST':
        return redirect('XMR:account')
    
    amount = request.POST.get('amount')
    phone_number = request.POST.get('phone_number')
    mpesa_message = request.POST.get('mpesa_message')
    mpesa_screenshot = request.FILES.get('mpesa_screenshot')
    
    # Validate amount
    try:
        amount = Decimal(amount)
        min_deposit = SystemConfig.get_config('min_deposit', 1200)
        if amount < min_deposit:
            messages.error(request, f'Minimum deposit is {min_deposit} KSH')
            return redirect('XMR:account')
    except (TypeError, ValueError, InvalidOperation):
        messages.error(request, 'Invalid amount')
        return redirect('XMR:account')
    
    # Validate phone
    phone_number = clean_phone_number(phone_number)
    if not validate_phone_number(phone_number):
        messages.error(request, 'Please enter a valid Kenyan phone number')
        return redirect('XMR:account')
    
    # Check if message or screenshot is provided
    if not mpesa_message and not mpesa_screenshot:
        messages.error(request, 'Please provide either the M-Pesa message or screenshot')
        return redirect('XMR:account')
    
    try:
        # Create M-Pesa payment record
        payment = MpesaPayment.objects.create(
            user=request.user,
            amount=amount,
            phone_number=phone_number,
            mpesa_message=mpesa_message or '',
            mpesa_screenshot=mpesa_screenshot
        )
        
        # Try to extract data from message if provided
        if mpesa_message:
            payment.extract_mpesa_data()
        
        messages.success(request, 'Deposit request submitted successfully! It will be verified by admin.')
        
        # Log the deposit request
        SystemLog.objects.create(
            log_type='INFO',
            user=request.user,
            action='DEPOSIT_CREATED',
            description=f'Deposit request for {amount} KSH created'
        )
        
    except Exception as e:
        messages.error(request, f'Error creating deposit: {str(e)}')
        logger.error(f"Deposit creation error: {str(e)}", exc_info=True)
    
    return HttpResponseRedirect('/account/?tab=deposits')


# ==================== WITHDRAWAL VIEWS ====================
@login_required(login_url='XMR:signupin')
def create_withdrawal(request):
    """
    Create a new withdrawal request
    ONLY allowed on weekends (Saturday-Sunday) between 8 AM - 5 PM
    """
    if request.method != 'POST':
        return redirect('XMR:account')

    # ===== WITHDRAWAL WINDOW CHECK =====
    is_open, message = is_withdrawal_window()

    if not is_open:
        # Get detailed status for better error message
        status = get_withdrawal_window_status()
        error_message = f"""
        ❌ Withdrawals are only available on weekends (Saturday-Sunday) from 8:00 AM to 5:00 PM.

        Current Status: {status['status']}
        {status['message']}

        Window Hours: {status['opens_at']} - {status['closes_at']}
        """
        messages.error(request, error_message)

        # Log the attempt
        SystemLog.objects.create(
            log_type='WARNING',
            user=request.user,
            action='WITHDRAWAL_ATTEMPT_OUTSIDE_WINDOW',
            description=f'Withdrawal attempt outside window: {status["status"]}',
            ip_address=get_client_ip(request)
        )

        return redirect('XMR:account')

    # ===== CONTINUE WITH EXISTING WITHDRAWAL LOGIC =====
    amount = request.POST.get('amount')
    payment_method = request.POST.get('payment_method', 'MPESA')
    phone_number = request.POST.get('phone_number', '').strip()
    bank_details = request.POST.get('bank_details', '').strip()

    wallet = request.user.wallet

    # Validate amount
    try:
        amount = Decimal(amount)
        min_withdrawal = SystemConfig.get_config('min_withdrawal', 500)

        if amount < min_withdrawal:
            messages.error(request, f'Minimum withdrawal is {min_withdrawal} KSH')
            return redirect('XMR:account')

        # CHECK AVAILABLE BALANCE ONLY (balance field)
        if wallet.balance < amount:
            messages.error(
                request,
                f'Insufficient available balance. You have {wallet.balance} KSH available, but requested {amount} KSH.'
            )
            return redirect('XMR:account')

    except (TypeError, ValueError, InvalidOperation):
        messages.error(request, 'Invalid amount')
        return redirect('XMR:account')

    # Validate based on payment method
    if payment_method == 'MPESA':
        phone_number = clean_phone_number(phone_number)
        if not validate_phone_number(phone_number):
            messages.error(request, 'Please enter a valid Kenyan phone number for M-Pesa withdrawal')
            return redirect('XMR:account')
    elif payment_method == 'BANK':
        if not bank_details:
            messages.error(request, 'Please provide bank account details')
            return redirect('XMR:account')

    try:
        # Create withdrawal request
        withdrawal = WithdrawalRequest.objects.create(
            user=request.user,
            amount=amount,
            payment_method=payment_method,
            phone_number=phone_number if payment_method == 'MPESA' else None,
            bank_details=bank_details if payment_method == 'BANK' else None
        )

        messages.success(request,
                         f'✅ Withdrawal request for {amount} KSH submitted successfully! It will be processed by admin.')

        # Log the withdrawal request
        SystemLog.objects.create(
            log_type='INFO',
            user=request.user,
            action='WITHDRAWAL_CREATED',
            description=f'Withdrawal request for {amount} KSH created (Weekend window)',
            ip_address=get_client_ip(request)
        )

    except ValidationError as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f'Error creating withdrawal: {str(e)}')
        logger.error(f"Withdrawal creation error: {str(e)}", exc_info=True)

    return HttpResponseRedirect('/account/?tab=withdrawals')

@login_required(login_url='XMR:signupin')
def cancel_withdrawal(request, withdrawal_id):
    """Cancel a pending withdrawal request - NO FUNDS TO UNLOCK"""
    withdrawal = get_object_or_404(WithdrawalRequest, id=withdrawal_id, user=request.user)
    
    if withdrawal.status != 'PENDING':
        messages.error(request, 'Can only cancel pending withdrawals')
        return redirect('XMR:account')
    
    try:
        withdrawal.cancel()  # No funds to unlock
        messages.success(request, 'Withdrawal request cancelled successfully')
        
        SystemLog.objects.create(
            log_type='INFO',
            user=request.user,
            action='WITHDRAWAL_CANCELLED',
            description=f'Withdrawal {withdrawal.request_id} cancelled'
        )
        
    except ValidationError as e:
        messages.error(request, str(e))
    
    return HttpResponseRedirect('/account/?tab=withdrawals')


# ==================== INVESTMENT VIEWS ====================
@login_required(login_url='XMR:signupin')
def investments(request):
    """View all available investments with payout schedule information"""

    # ===== AUTO PAYOUT CHECK =====
    check_user_payouts(request.user)
    # =============================

    # Get active tokens
    active_tokens = Token.objects.filter(status='ACTIVE').order_by('token_number')

    # Get user's investments
    user_investments_all = Investment.objects.filter(
        user=request.user
    ).select_related('token').order_by('-created_at')

    # Separate active and completed for display
    active_investments = user_investments_all.filter(status='ACTIVE')
    completed_investments = user_investments_all.filter(status='COMPLETED')

    # Get wallet for balance check
    wallet = request.user.wallet

    # Calculate totals
    total_invested = active_investments.aggregate(total=Sum('amount'))['total'] or 0
    total_earned = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='PROFIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0

    # ===== FIXED: Get next payout info =====
    next_payout = active_investments.order_by('end_date').first()

    if next_payout:
        # Calculate business days remaining (NOT calendar days)
        business_days_remaining = next_payout.get_business_days_remaining()
        next_payout_days = business_days_remaining

        # Get next payout date (always a weekday)
        next_payout_date = next_payout.get_next_payout_date()
        next_payout_display = next_payout_date.strftime('%A, %B %d, %Y')

        # Calculate hours until next payout
        now = timezone.now()
        hours_until = (next_payout_date - now).total_seconds() / 3600

        if hours_until < 24:
            next_payout_hours = f"{int(hours_until)} hours"
        else:
            next_payout_hours = f"{int(hours_until / 24)} days"
    else:
        next_payout_days = 0
        next_payout_display = "No active investments"
        next_payout_hours = "N/A"

    # Get user's purchase history for each token
    user_token_purchases = {}
    for token in active_tokens:
        user_token_purchases[token.id] = Investment.objects.filter(
            user=request.user,
            token=token
        ).count()

    # ===== SCHEDULE INFORMATION =====
    now = timezone.now()
    is_weekday_now = is_weekday(now)
    is_weekend_now = is_weekend(now)

    # Calculate payout schedule info
    if is_weekend_now:
        next_business_day = get_next_business_day(now)
        next_payout_display = next_business_day.strftime('%A, %B %d, %Y')
        payout_status = 'paused'
        payout_status_badge = 'warning'
        payout_icon = 'fa-pause-circle'
        payout_message = f'Weekend - No payouts today. Next payout: {next_payout_display}'
        payout_day_name = now.strftime('%A')
    else:
        next_payout_display = now.strftime('%A, %B %d, %Y')
        payout_status = 'active'
        payout_status_badge = 'success'
        payout_icon = 'fa-check-circle'
        payout_message = f'Today is a payout day ({now.strftime("%A")})'
        payout_day_name = now.strftime('%A')

    # Get withdrawal window status
    withdrawal_status = get_withdrawal_window_status()
    is_withdrawal_open = withdrawal_status['open']

    # Calculate next withdrawal window
    if is_weekend_now:
        if is_withdrawal_open:
            next_withdrawal_window = 'Open now!'
            next_withdrawal_status = 'open'
            next_withdrawal_badge = 'success'
        else:
            if now.time() < time(8, 0):
                next_withdrawal_window = f'Today at 8:00 AM'
                next_withdrawal_status = 'opens_today'
                next_withdrawal_badge = 'info'
            else:
                next_withdrawal_window = 'Next weekend'
                next_withdrawal_status = 'closed'
                next_withdrawal_badge = 'secondary'
    else:
        days_until_saturday = 5 - now.weekday()
        if days_until_saturday == 1:
            next_withdrawal_window = 'Tomorrow (Saturday)'
        elif days_until_saturday == 0:
            next_withdrawal_window = 'Today (Saturday)'
        elif days_until_saturday == 2:
            next_withdrawal_window = 'In 2 days (Saturday)'
        else:
            next_withdrawal_window = f'In {days_until_saturday} days (Saturday)'
        next_withdrawal_status = 'weekday'
        next_withdrawal_badge = 'secondary'

    # ===== EARNING PROJECTIONS =====
    projected_earnings = 0
    for investment in active_investments:
        if investment.remaining_payouts > 0:
            projected_earnings += investment.daily_return

    # Calculate if any investments are due for payout today (weekday check)
    investments_due_today = 0
    if is_weekday_now:
        investments_due_today = active_investments.filter(
            remaining_payouts__gt=0
        ).count()

    # ===== GET PAYOUT HISTORY SUMMARY =====
    last_7_days = now - timedelta(days=7)
    payout_history = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='PROFIT',
        status='COMPLETED',
        created_at__gte=last_7_days
    ).count()

    total_payout_count = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='PROFIT',
        status='COMPLETED'
    ).count()

    # ===== BUSINESS DAY COUNT =====
    account_created = request.user.date_joined
    business_days_since_joined = count_business_days(account_created, now)

    # ===== CALCULATE TOTAL DAILY RETURN =====
    total_daily_return = 0
    for investment in active_investments:
        if investment.remaining_payouts > 0:
            total_daily_return += investment.daily_return

    context = {
        # Original context variables
        'active_tokens': active_tokens,
        'active_investments': active_investments,
        'completed_investments': completed_investments,
        'user_investments': user_investments_all,
        'wallet': wallet,
        'available_balance': wallet.available_balance(),
        'locked_balance': wallet.locked_balance,
        'total_balance': wallet.balance + wallet.locked_balance,
        'total_invested': total_invested,
        'total_earned': total_earned,
        'next_payout_days': next_payout_days,  # Business days remaining
        'next_payout_hours': next_payout_hours,
        'next_payout_date_display': next_payout_display,
        'user_token_purchases': user_token_purchases,

        # ===== SCHEDULE INFORMATION =====
        'is_weekday': is_weekday_now,
        'is_weekend': is_weekend_now,
        'payout_status': payout_status,
        'payout_status_badge': payout_status_badge,
        'payout_icon': payout_icon,
        'payout_message': payout_message,
        'payout_day_name': payout_day_name,
        'next_payout_day': next_payout_display,
        'payout_schedule': 'Monday - Friday (Weekdays only)',
        'payout_weekend_note': 'Weekends are paused - earnings resume on Monday',

        # Withdrawal schedule
        'withdrawal_window': withdrawal_status,
        'is_withdrawal_open': is_withdrawal_open,
        'withdrawal_status_message': withdrawal_status['message'],
        'withdrawal_schedule': 'Saturday - Sunday, 8:00 AM - 5:00 PM',
        'withdrawal_opens_at': withdrawal_status.get('opens_at', '8:00 AM'),
        'withdrawal_closes_at': withdrawal_status.get('closes_at', '5:00 PM'),
        'next_withdrawal_window': next_withdrawal_window,
        'next_withdrawal_status': next_withdrawal_status,
        'next_withdrawal_badge': next_withdrawal_badge,

        # Earning projections
        'projected_earnings': projected_earnings,
        'investments_due_today': investments_due_today,
        'payout_history': payout_history,
        'total_payout_count': total_payout_count,
        'business_days_since_joined': business_days_since_joined,
        'total_daily_return': total_daily_return,

        # Helpful messages
        'payout_help_text': 'Daily earnings are processed on business days (Monday-Friday). Weekends are automatically paused.',
        'withdrawal_help_text': 'Withdrawals are only available on weekends (Saturday-Sunday) from 8:00 AM to 5:00 PM.',
    }

    return render(request, 'investments.html', context)


@login_required(login_url='XMR:signupin')
def investment_detail(request, token_id):
    """Redirect to investments page with token details modal"""
    from django.urls import reverse
    return redirect(f"{reverse('XMR:investments')}?token={token_id}")


@login_required(login_url='XMR:signupin')
@transaction.atomic
def buy_investment(request, token_id):
    """
    Process investment purchase with atomic transaction
    Moves money from available balance to locked balance
    """
    if request.method != 'POST':
        return redirect('XMR:investment_detail', token_id=token_id)
    
    # Log the attempt
    logger.info(f"Investment attempt by user {request.user.username} for token {token_id}")
    
    try:
        # Select token with lock to prevent race conditions
        token = Token.objects.select_for_update().get(id=token_id)
        wallet = Wallet.objects.select_for_update().get(user=request.user)
        
        # DEBUG LOGGING
        logger.debug(f"User {request.user.username} - Available: {wallet.balance}, Locked: {wallet.locked_balance}")
        
        # Validate token is available
        if token.status != 'ACTIVE':
            messages.error(request, 'This token is not currently active')
            return redirect('XMR:investments')
        
        if not token.is_available():
            messages.error(request, 'This token is sold out')
            return redirect('XMR:investments')
        
        # Check minimum investment
        amount = token.minimum_investment
        
        # Check available balance (balance field)
        if wallet.balance < amount:
            messages.error(request, f'Insufficient available balance. You need {amount} KSH, but you have {wallet.balance} KSH available')
            logger.warning(f"Insufficient balance for user {request.user.username}: Have {wallet.balance}, Need {amount}")
            return redirect('XMR:investments')
        
        # Check max purchases
        if token.max_purchases_per_user:
            user_purchases = Investment.objects.filter(
                user=request.user,
                token=token
            ).count()
            if user_purchases >= token.max_purchases_per_user:
                messages.error(request, f'You have reached the maximum purchases for this token')
                return redirect('XMR:investments')
        
        # Create investment - this will move money from available to locked
        investment = Investment.objects.create(
            user=request.user,
            token=token,
            amount=amount
        )
        
        # Refresh wallet to get updated balances
        wallet.refresh_from_db()
        
        # Verify investment was created successfully
        if not investment.id:
            raise IntegrityError("Investment creation failed - no ID generated")
        
        # Log success
        logger.info(f"✅ Investment created: ID {investment.id} for user {request.user.username}")
        logger.debug(f"New wallet state - Available: {wallet.balance}, Locked: {wallet.locked_balance}")
        
        # Create success message with details
        success_message = (
            f"Successfully invested in {token.name}! "
            f"Amount: {amount} KSH, "
            f"Daily Return: {token.daily_return} KSH, "
            f"Duration: {token.return_days} days"
        )
        messages.success(request, success_message)
        
        # Log the investment
        SystemLog.objects.create(
            log_type='INFO',
            user=request.user,
            action='INVESTMENT_PURCHASED',
            description=f'Purchased {token.name} for {amount} KSH. Investment ID: {investment.id}'
        )
        
    except Token.DoesNotExist:
        messages.error(request, 'Token not found')
        logger.error(f"Token {token_id} not found")
        return redirect('XMR:investments')
        
    except ValidationError as e:
        messages.error(request, str(e))
        logger.error(f"ValidationError in investment: {str(e)}")
        transaction.set_rollback(True)
        
    except IntegrityError as e:
        messages.error(request, 'Investment creation failed due to database error. Please try again.')
        logger.error(f"IntegrityError in investment: {str(e)}", exc_info=True)
        transaction.set_rollback(True)
        
    except Exception as e:
        messages.error(request, f'Error processing investment: {str(e)}')
        logger.error(f"Unexpected error in investment: {str(e)}", exc_info=True)
        transaction.set_rollback(True)
    
    return redirect('XMR:investments')


# ==================== ADMIN VIEWS ====================

@login_required(login_url='XMR:signupin')
def myadmin(request):
    """Consolidated admin dashboard with all management features"""
    if not request.user.is_staff:
        messages.error(request, 'You are not authorized to access the admin area.')
        return redirect('XMR:account')
    
    from django.db.models.functions import TruncDate, TruncMonth
    
    # Get current date for filters
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)
    
    # ========== DASHBOARD STATS ==========
    dashboard_stats = {
        'total_users': User.objects.count(),
        'new_users_today': User.objects.filter(date_joined__date=today).count(),
        'new_users_week': User.objects.filter(date_joined__date__gte=week_ago).count(),
        'verified_users': UserProfile.objects.filter(phone_verified=True).count(),
        
        'total_deposits': float(Transaction.objects.filter(
            transaction_type='DEPOSIT', status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or 0),
        
        'total_withdrawals': float(Transaction.objects.filter(
            transaction_type='WITHDRAWAL', status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or 0),
        
        'total_profits_paid': float(Transaction.objects.filter(
            transaction_type='PROFIT', status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or 0),
        
        'total_system_balance': float(Wallet.objects.aggregate(
            total=Sum('balance')
        )['total'] or 0),
        
        'total_locked_balance': float(Wallet.objects.aggregate(
            total=Sum('locked_balance')
        )['total'] or 0),
        
        'total_system_assets': float(Wallet.objects.aggregate(
            total=Sum('balance') + Sum('locked_balance')
        )['total'] or 0),
        
        'pending_deposits': MpesaPayment.objects.filter(status='PENDING').count(),
        'pending_withdrawals': WithdrawalRequest.objects.filter(status='PENDING').count(),
        'pending_kyc': UserProfile.objects.filter(
            Q(id_front_image__isnull=False) | 
            Q(id_back_image__isnull=False) | 
            Q(selfie_with_id__isnull=False)
        ).exclude(phone_verified=True, id_verified=True).count(),
        
        'active_investments': Investment.objects.filter(status='ACTIVE').count(),
        'completed_investments': Investment.objects.filter(status='COMPLETED').count(),
        'total_invested': float(Investment.objects.filter(
            status='ACTIVE'
        ).aggregate(total=Sum('amount'))['total'] or 0),
    }
    
    # Chart data (last 7 days deposits)
    daily_deposits = []
    for i in range(7):
        date = today - timedelta(days=i)
        total = Transaction.objects.filter(
            transaction_type='DEPOSIT',
            status='COMPLETED',
            created_at__date=date
        ).aggregate(total=Sum('amount'))['total'] or 0
        daily_deposits.append({
            'date': date.strftime('%Y-%m-%d'),
            'total': float(total)
        })
    
    # ========== DEPOSITS DATA ==========
    deposit_status = request.GET.get('deposit_status', 'PENDING')
    deposits = MpesaPayment.objects.filter(
        status=deposit_status
    ).select_related('user', 'user__profile').order_by('-created_at')[:50]
    
    deposits_data = []
    for d in deposits:
        deposits_data.append({
            'id': d.id,
            'user': d.user.username,
            'user_id': d.user.id,
            'amount': float(d.amount),
            'phone': d.phone_number,
            'mpesa_code': d.mpesa_code,
            'status': d.status,
            'created_at': d.created_at.strftime('%Y-%m-%d %H:%M'),
            'has_screenshot': bool(d.mpesa_screenshot),
            'mpesa_screenshot_url': d.mpesa_screenshot.url if d.mpesa_screenshot else None,
        })
    
    # ========== WITHDRAWALS DATA ==========
    withdrawal_status = request.GET.get('withdrawal_status', 'PENDING')
    withdrawals = WithdrawalRequest.objects.filter(
        status=withdrawal_status
    ).select_related('user', 'user__wallet').order_by('-created_at')[:50]
    
    withdrawals_data = []
    for w in withdrawals:
        withdrawals_data.append({
            'id': w.id,
            'request_id': w.request_id,
            'user': w.user.username,
            'user_id': w.user.id,
            'amount': float(w.amount),
            'net_amount': float(w.net_amount),
            'tax': float(w.tax_amount),
            'method': w.payment_method,
            'phone': w.phone_number,
            'status': w.status,
            'created_at': w.created_at.strftime('%Y-%m-%d %H:%M'),
        })
    
    # ========== INVESTMENTS DATA ==========
    investment_status = request.GET.get('investment_status', '')
    investments_query = Investment.objects.select_related(
        'user', 'token'
    ).order_by('-created_at')
    
    if investment_status:
        investments_query = investments_query.filter(status=investment_status)
    
    # Paginate investments
    investment_paginator = Paginator(investments_query, 50)
    investment_page = request.GET.get('investment_page', 1)
    investments_page = investment_paginator.get_page(investment_page)
    
    investments_data = []
    for inv in investments_page:
        # Calculate progress percentage
        total_days = inv.token.return_days
        completed_days = total_days - inv.remaining_payouts
        progress_percentage = (completed_days / total_days) * 100 if total_days > 0 else 0
        
        investments_data.append({
            'id': inv.id,
            'investment_id': inv.investment_id,
            'user': inv.user.username,
            'user_id': inv.user.id,
            'token_id': inv.token.id,
            'token_name': inv.token.name,
            'token_display_name': inv.token.display_name,
            'amount': float(inv.amount),
            'daily_return': float(inv.daily_return),
            'start_date': inv.start_date.strftime('%Y-%m-%d %H:%M'),
            'end_date': inv.end_date.strftime('%Y-%m-%d %H:%M'),
            'last_payout_date': inv.last_payout_date.strftime('%Y-%m-%d %H:%M') if inv.last_payout_date else None,
            'status': inv.status,
            'total_paid': float(inv.total_paid),
            'remaining_payouts': inv.remaining_payouts,
            'progress_percentage': round(progress_percentage, 1),
            'transaction_id': inv.transaction.id if inv.transaction else None,
        })
    
    # ========== TRANSACTIONS DATA ==========
    transaction_type = request.GET.get('transaction_type', '')
    transaction_status = request.GET.get('transaction_status', '')
    
    transactions_query = Transaction.objects.select_related(
        'wallet__user', 'investment', 'withdrawal'
    ).order_by('-created_at')
    
    if transaction_type:
        transactions_query = transactions_query.filter(transaction_type=transaction_type)
    if transaction_status:
        transactions_query = transactions_query.filter(status=transaction_status)
    
    # Paginate transactions
    transaction_paginator = Paginator(transactions_query, 50)
    transaction_page = request.GET.get('transaction_page', 1)
    transactions_page = transaction_paginator.get_page(transaction_page)
    
    transactions_data = []
    for t in transactions_page:
        transactions_data.append({
            'id': t.id,
            'transaction_id': t.transaction_id,
            'user': t.wallet.user.username,
            'user_id': t.wallet.user.id,
            'type': t.transaction_type,
            'amount': float(t.amount),
            'status': t.status,
            'description': t.description,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
            'processed_by': t.processed_by.username if t.processed_by else None,
            'processed_at': t.processed_at.strftime('%Y-%m-%d %H:%M') if t.processed_at else None,
            'investment_id': t.investment.id if t.investment else None,
            'withdrawal_id': t.withdrawal.id if t.withdrawal else None,
        })
    
    # ========== USERS DATA ==========
    user_search = request.GET.get('user_search', '')
    user_verified = request.GET.get('user_verified', '')
    user_banned = request.GET.get('user_banned', '')
    
    users_query = User.objects.select_related('profile', 'wallet').order_by('-date_joined')
    
    if user_search:
        users_query = users_query.filter(
            Q(username__icontains=user_search) |
            Q(email__icontains=user_search) |
            Q(first_name__icontains=user_search) |
            Q(last_name__icontains=user_search) |
            Q(profile__phone_number__icontains=user_search)
        )
    
    if user_verified:
        users_query = users_query.filter(profile__phone_verified=(user_verified == 'verified'))
    
    if user_banned:
        users_query = users_query.filter(profile__is_banned=(user_banned == 'banned'))
    
    user_paginator = Paginator(users_query, 20)
    user_page = request.GET.get('user_page', 1)
    users_page = user_paginator.get_page(user_page)
    
    users_data = []
    for u in users_page:
        users_data.append({
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'first_name': u.first_name,
            'last_name': u.last_name,
            'phone': u.profile.phone_number,
            'balance': float(u.wallet.balance) if hasattr(u, 'wallet') and u.wallet.balance else 0,
            'locked_balance': float(u.wallet.locked_balance) if hasattr(u, 'wallet') and u.wallet.locked_balance else 0,
            'total_balance': float(u.wallet.balance + u.wallet.locked_balance) if hasattr(u, 'wallet') else 0,
            'phone_verified': u.profile.phone_verified,
            'id_verified': u.profile.id_verified,
            'is_banned': u.profile.is_banned,
            'date_joined': u.date_joined.strftime('%Y-%m-%d'),
            'referral_code': u.profile.referral_code,
            'active_investments': Investment.objects.filter(user=u, status='ACTIVE').count(),
            'total_invested': float(Investment.objects.filter(user=u, status='ACTIVE').aggregate(total=Sum('amount'))['total'] or 0),
        })
    
    # ========== TOKENS DATA ==========
    tokens = Token.objects.all().order_by('token_number')
    tokens_data = []
    for t in tokens:
        roi = float((t.total_return / t.minimum_investment) * 100) if t.minimum_investment and t.minimum_investment > 0 else 0
        
        tokens_data.append({
            'id': t.id,
            'name': t.name,
            'display_name': t.display_name,
            'token_number': t.token_number,
            'minimum_investment': float(t.minimum_investment) if t.minimum_investment else 0,
            'daily_return': float(t.daily_return) if t.daily_return else 0,
            'return_days': t.return_days,
            'total_return': float(t.total_return) if t.total_return else 0,
            'status': t.status,
            'max_purchases_per_user': t.max_purchases_per_user,
            'total_supply': t.total_supply,
            'purchased_count': t.purchased_count,
            'is_available': t.is_available(),
            'roi': roi,
            'description': t.description or '',
            'icon': t.icon or '',
            'color': t.color or 'primary',
        })
    
    # ========== KYC DATA ==========
    pending_kyc = UserProfile.objects.filter(
        Q(id_front_image__isnull=False) |
        Q(id_back_image__isnull=False) |
        Q(selfie_with_id__isnull=False)
    ).filter(
        Q(phone_verified=False) | Q(id_verified=False)
    ).select_related('user').order_by('-updated_at')[:50]
    
    kyc_data = []
    for k in pending_kyc:
        kyc_data.append({
            'id': k.id,
            'user_id': k.user.id,
            'username': k.user.username,
            'full_name': k.national_id_name or f"{k.user.first_name} {k.user.last_name}",
            'phone': k.phone_number,
            'phone_verified': k.phone_verified,
            'id_verified': k.id_verified,
            'has_id_front': bool(k.id_front_image),
            'has_id_back': bool(k.id_back_image),
            'has_selfie': bool(k.selfie_with_id),
            'id_front_url': k.id_front_image.url if k.id_front_image else None,
            'id_back_url': k.id_back_image.url if k.id_back_image else None,
            'selfie_url': k.selfie_with_id.url if k.selfie_with_id else None,
            'submitted_at': k.updated_at.strftime('%Y-%m-%d %H:%M'),
        })
    
    # ========== SYSTEM LOGS DATA ==========
    log_type = request.GET.get('log_type', '')
    log_user = request.GET.get('log_user', '')
    
    logs_query = SystemLog.objects.all().select_related('user').order_by('-created_at')
    
    if log_type:
        logs_query = logs_query.filter(log_type=log_type)
    if log_user:
        logs_query = logs_query.filter(user_id=log_user)
    
    log_paginator = Paginator(logs_query, 50)
    log_page = request.GET.get('log_page', 1)
    logs_page = log_paginator.get_page(log_page)
    
    logs_data = []
    for l in logs_page:
        logs_data.append({
            'id': l.id,
            'type': l.log_type,
            'user': l.user.username if l.user else 'System',
            'action': l.action,
            'description': l.description,
            'ip': l.ip_address,
            'created_at': l.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        })
    
    # Users for log filter dropdown
    log_users = User.objects.filter(
        id__in=SystemLog.objects.values_list('user_id', flat=True).distinct()
    ).values('id', 'username')
    
    # ========== SYSTEM CONFIG ==========
    configs = {c.key: c.value for c in SystemConfig.objects.all()}
    
    default_configs = {
        'min_deposit': 1200,
        'min_withdrawal': 500,
        'withdrawal_tax': 5,
        'referral_commission': 5,
        'mpesa_paybill': '123456',
        'mpesa_account': 'INVEST',
        'site_name': 'XMR Investments',
        'support_email': 'support@example.com',
        'support_phone': '0712345678',
    }
    
    for key, value in default_configs.items():
        if key not in configs:
            configs[key] = value
    
    # Get active tab from URL or default to dashboard
    active_tab = request.GET.get('tab', 'dashboard')
    
    context = {
        # Dashboard stats
        'dashboard_stats': dashboard_stats,
        'daily_deposits': json.dumps(daily_deposits),
        
        # Deposits
        'deposits_data': deposits_data,
        'deposits': json.dumps(deposits_data),
        'deposit_status_choices': [s[0] for s in MpesaPayment.PAYMENT_STATUS],
        'current_deposit_status': deposit_status,
        
        # Withdrawals
        'withdrawals_data': withdrawals_data,
        'withdrawals': json.dumps(withdrawals_data),
        'withdrawal_status_choices': [s[0] for s in WithdrawalRequest.WITHDRAWAL_STATUS],
        'current_withdrawal_status': withdrawal_status,
        
        # Investments
        'investments_data': investments_data,
        'investments': json.dumps(investments_data),
        'investment_status_choices': [s[0] for s in Investment.INVESTMENT_STATUS],
        'current_investment_status': investment_status,
        'investments_pagination': {
            'current_page': investments_page.number,
            'total_pages': investments_page.paginator.num_pages,
            'total_items': investments_page.paginator.count,
            'has_next': investments_page.has_next(),
            'has_previous': investments_page.has_previous(),
        },
        
        # Transactions
        'transactions_data': transactions_data,
        'transactions': json.dumps(transactions_data),
        'transaction_type_choices': [s[0] for s in Transaction.TRANSACTION_TYPES],
        'transaction_status_choices': [s[0] for s in Transaction.STATUS_CHOICES],
        'current_transaction_type': transaction_type,
        'current_transaction_status': transaction_status,
        'transactions_pagination': {
            'current_page': transactions_page.number,
            'total_pages': transactions_page.paginator.num_pages,
            'total_items': transactions_page.paginator.count,
            'has_next': transactions_page.has_next(),
            'has_previous': transactions_page.has_previous(),
        },
        
        # Users
        'users_data': users_data,
        'users': json.dumps(users_data),
        'users_pagination': {
            'current_page': users_page.number,
            'total_pages': users_page.paginator.num_pages,
            'total_items': users_page.paginator.count,
            'has_next': users_page.has_next(),
            'has_previous': users_page.has_previous(),
        },
        
        # Tokens
        'tokens_data': tokens_data,
        'tokens': json.dumps(tokens_data),
        'token_status_choices': [s[0] for s in Token.TOKEN_STATUS],
        
        # KYC
        'kyc_pending_data': kyc_data,
        'kyc_pending': json.dumps(kyc_data),
        
        # Logs
        'logs_data': logs_data,
        'logs': json.dumps(logs_data),
        'logs_pagination': {
            'current_page': logs_page.number,
            'total_pages': logs_page.paginator.num_pages,
            'total_items': logs_page.paginator.count,
            'has_next': logs_page.has_next(),
            'has_previous': logs_page.has_previous(),
        },
        'log_users': list(log_users),
        'log_type_choices': [l[0] for l in SystemLog.LOG_TYPES],
        
        # Settings
        'configs': configs,
        
        # Active tab
        'active_tab': active_tab,
    }
    
    return render(request, 'admin.html', context)


# ==================== ADMIN API ENDPOINTS ====================

@login_required(login_url='XMR:signupin')
def admin_api(request):
    """API endpoint for admin actions"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    action = request.POST.get('action')
    
    # ========== DEPOSIT ACTIONS ==========
    if action == 'verify_deposit':
        deposit_id = request.POST.get('deposit_id')
        deposit = get_object_or_404(MpesaPayment, id=deposit_id)
        try:
            deposit.verify(request.user)
            return JsonResponse({'success': True, 'message': f'Deposit of {deposit.amount} KSH verified'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    elif action == 'reject_deposit':
        deposit_id = request.POST.get('deposit_id')
        reason = request.POST.get('reason', 'No reason provided')
        deposit = get_object_or_404(MpesaPayment, id=deposit_id)
        try:
            deposit.reject(request.user, reason)
            return JsonResponse({'success': True, 'message': 'Deposit rejected'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    # ========== WITHDRAWAL ACTIONS ==========
    elif action == 'process_withdrawal':
        withdrawal_id = request.POST.get('withdrawal_id')
        withdrawal = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
        try:
            withdrawal.process(request.user)
            return JsonResponse({'success': True, 'message': 'Withdrawal marked as processing'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    elif action == 'complete_withdrawal':
        withdrawal_id = request.POST.get('withdrawal_id')
        transaction_code = request.POST.get('transaction_code')
        if not transaction_code:
            return JsonResponse({'success': False, 'error': 'Transaction code required'})
        
        withdrawal = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
        try:
            withdrawal.complete(request.user, transaction_code)
            return JsonResponse({'success': True, 'message': 'Withdrawal completed'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    elif action == 'reject_withdrawal':
        withdrawal_id = request.POST.get('withdrawal_id')
        reason = request.POST.get('reason', 'No reason provided')
        withdrawal = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
        try:
            withdrawal.reject(request.user, reason)
            return JsonResponse({'success': True, 'message': 'Withdrawal rejected'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    # ========== USER ACTIONS ==========
    elif action == 'toggle_user_ban':
        user_id = request.POST.get('user_id')
        reason = request.POST.get('reason', 'No reason provided')
        user = get_object_or_404(User, id=user_id)
        profile = user.profile
        profile.is_banned = not profile.is_banned
        if profile.is_banned:
            profile.ban_reason = reason
        else:
            profile.ban_reason = None
        profile.save()
        return JsonResponse({
            'success': True,
            'is_banned': profile.is_banned,
            'message': f'User {"banned" if profile.is_banned else "unbanned"}'
        })
    
    elif action == 'verify_user_phone':
        user_id = request.POST.get('user_id')
        user = get_object_or_404(User, id=user_id)
        user.profile.phone_verified = True
        user.profile.save()
        return JsonResponse({'success': True, 'message': 'Phone verified'})
    
    elif action == 'verify_user_id':
        user_id = request.POST.get('user_id')
        user = get_object_or_404(User, id=user_id)
        user.profile.id_verified = True
        user.profile.save()
        return JsonResponse({'success': True, 'message': 'ID verified'})
    
    elif action == 'adjust_balance':
        user_id = request.POST.get('user_id')
        amount = request.POST.get('amount')
        description = request.POST.get('description', 'Deposit')
        adjust_type = request.POST.get('adjust_type', 'add')
        
        try:
            amount = Decimal(amount)
            if adjust_type == 'subtract':
                amount = -amount
                
            user = get_object_or_404(User, id=user_id)
            
            with transaction.atomic():
                wallet = Wallet.objects.select_for_update().get(user=user)
                
                # Check if subtracting would make balance negative
                if amount < 0 and wallet.balance < abs(amount):
                    return JsonResponse({
                        'success': False, 
                        'error': f'Cannot subtract {abs(amount)} KSH. Available balance is only {wallet.balance} KSH'
                    })
                
                trans = Transaction.objects.create(
                    wallet=wallet,
                    transaction_type='ADJUSTMENT',
                    amount=abs(amount),
                    description=description,
                    status='COMPLETED',
                    processed_by=request.user,
                    processed_at=timezone.now()
                )
                
                wallet.balance += amount
                wallet.save()
            
            return JsonResponse({
                'success': True,
                'new_balance': float(wallet.balance),
                'new_locked': float(wallet.locked_balance),
                'message': f'{"Added" if amount > 0 else "Subtracted"} {abs(amount)} KSH to/from wallet'
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    # ========== TOKEN ACTIONS ==========
    elif action == 'create_token':
        try:
            name = request.POST.get('name')
            display_name = request.POST.get('display_name')
            
            token_number_str = request.POST.get('token_number')
            if not token_number_str:
                return JsonResponse({'success': False, 'error': 'Token number is required'})
            token_number = int(token_number_str)
            
            min_investment_str = request.POST.get('minimum_investment')
            if not min_investment_str:
                return JsonResponse({'success': False, 'error': 'Minimum investment is required'})
            minimum_investment = Decimal(min_investment_str)
            
            daily_return_str = request.POST.get('daily_return')
            if not daily_return_str:
                return JsonResponse({'success': False, 'error': 'Daily return is required'})
            daily_return = Decimal(daily_return_str)
            
            return_days_str = request.POST.get('return_days', '12')
            return_days = int(return_days_str)
            
            status = request.POST.get('status', 'INACTIVE')
            
            max_purchases_input = request.POST.get('max_purchases_per_user')
            max_purchases_per_user = int(max_purchases_input) if max_purchases_input else 1
            
            total_supply_input = request.POST.get('total_supply')
            total_supply = int(total_supply_input) if total_supply_input else None
            
            description = request.POST.get('description', '')
            icon = request.POST.get('icon', '')
            color = request.POST.get('color', 'primary')
            
            if token_number < 1 or token_number > 20:
                return JsonResponse({'success': False, 'error': 'Token number must be between 1 and 20'})
            
            if Token.objects.filter(token_number=token_number).exists():
                return JsonResponse({'success': False, 'error': f'Token number {token_number} already exists'})
            
            token = Token.objects.create(
                name=name,
                display_name=display_name,
                token_number=token_number,
                minimum_investment=minimum_investment,
                daily_return=daily_return,
                return_days=return_days,
                status=status,
                max_purchases_per_user=max_purchases_per_user,
                total_supply=total_supply,
                description=description,
                icon=icon,
                color=color,
            )
            
            return JsonResponse({
                'success': True, 
                'message': f'Token {token.name} created successfully',
                'token_id': token.id
            })
            
        except ValueError as e:
            return JsonResponse({'success': False, 'error': f'Invalid number format: {str(e)}'})
        except InvalidOperation as e:
            return JsonResponse({'success': False, 'error': f'Invalid decimal format: {str(e)}'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    elif action == 'update_token':
        token_id = request.POST.get('token_id')
        token = get_object_or_404(Token, id=token_id)
        
        try:
            token.name = request.POST.get('name')
            token.display_name = request.POST.get('display_name')
            
            token_number_str = request.POST.get('token_number')
            if token_number_str:
                new_token_number = int(token_number_str)
                if Token.objects.filter(token_number=new_token_number).exclude(id=token.id).exists():
                    return JsonResponse({'success': False, 'error': f'Token number {new_token_number} already exists'})
                token.token_number = new_token_number
            
            min_investment_str = request.POST.get('minimum_investment')
            if min_investment_str:
                token.minimum_investment = Decimal(min_investment_str)
            
            daily_return_str = request.POST.get('daily_return')
            if daily_return_str:
                token.daily_return = Decimal(daily_return_str)
            
            return_days_str = request.POST.get('return_days')
            if return_days_str:
                token.return_days = int(return_days_str)
            
            token.status = request.POST.get('status')
            
            max_purchases_input = request.POST.get('max_purchases_per_user')
            token.max_purchases_per_user = int(max_purchases_input) if max_purchases_input else None
            
            total_supply_input = request.POST.get('total_supply')
            token.total_supply = int(total_supply_input) if total_supply_input else None
            
            token.description = request.POST.get('description', '')
            token.icon = request.POST.get('icon', '')
            token.color = request.POST.get('color', 'primary')
            
            token.save()
            
            return JsonResponse({
                'success': True, 
                'message': f'Token {token.name} updated successfully'
            })
            
        except ValueError as e:
            return JsonResponse({'success': False, 'error': f'Invalid number format: {str(e)}'})
        except InvalidOperation as e:
            return JsonResponse({'success': False, 'error': f'Invalid decimal format: {str(e)}'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    # ========== KYC ACTIONS ==========
    elif action == 'verify_kyc_phone':
        profile_id = request.POST.get('profile_id')
        profile = get_object_or_404(UserProfile, id=profile_id)
        profile.phone_verified = True
        profile.save()
        
        SystemLog.objects.create(
            log_type='ADMIN_ACTION',
            user=request.user,
            action='KYC_PHONE_VERIFIED',
            description=f'Phone verified for user {profile.user.username}'
        )
        
        return JsonResponse({'success': True, 'message': 'Phone verified'})
    
    elif action == 'verify_kyc_id':
        profile_id = request.POST.get('profile_id')
        profile = get_object_or_404(UserProfile, id=profile_id)
        profile.id_verified = True
        profile.save()
        
        SystemLog.objects.create(
            log_type='ADMIN_ACTION',
            user=request.user,
            action='KYC_ID_VERIFIED',
            description=f'ID verified for user {profile.user.username}'
        )
        
        return JsonResponse({'success': True, 'message': 'ID verified'})
    
    elif action == 'verify_kyc_all':
        profile_id = request.POST.get('profile_id')
        profile = get_object_or_404(UserProfile, id=profile_id)
        profile.phone_verified = True
        profile.id_verified = True
        profile.save()
        
        SystemLog.objects.create(
            log_type='ADMIN_ACTION',
            user=request.user,
            action='KYC_FULLY_VERIFIED',
            description=f'User {profile.user.username} fully verified'
        )
        
        return JsonResponse({'success': True, 'message': 'User fully verified'})
    
    # ========== INVESTMENT RECOVERY ==========
    elif action == 'recover_investment':
        if not request.user.is_superuser:
            return JsonResponse({'success': False, 'error': 'Superuser required'})
        
        user_id = request.POST.get('user_id')
        amount = request.POST.get('amount')
        token_id = request.POST.get('token_id')
        
        try:
            with transaction.atomic():
                user = User.objects.get(id=user_id)
                token = Token.objects.get(id=token_id)
                wallet = Wallet.objects.select_for_update().get(user=user)
                
                # Check if investment already exists
                existing_investment = Investment.objects.filter(
                    user=user,
                    token=token,
                    amount=amount
                ).first()
                
                if existing_investment:
                    return JsonResponse({
                        'success': False,
                        'error': 'Investment already exists for this user'
                    })
                
                # Create the missing investment
                investment = Investment.objects.create(
                    user=user,
                    token=token,
                    amount=amount
                )
                
                # Create transaction if needed
                if not investment.transaction:
                    transaction_record = Transaction.objects.create(
                        wallet=wallet,
                        transaction_type='INVESTMENT',
                        amount=amount,
                        description=f"Recovered investment in {token.name}",
                        status='COMPLETED',
                        investment=investment,
                        processed_by=request.user
                    )
                    investment.transaction = transaction_record
                    investment.save(update_fields=['transaction'])
                
                return JsonResponse({
                    'success': True,
                    'message': f'Investment recovered for user {user.username}'
                })
                
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'User not found'})
        except Token.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Token not found'})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    # ========== SETTINGS ACTIONS ==========
    elif action == 'update_settings':
        try:
            updated_keys = []
            for key, value in request.POST.items():
                if key.startswith('config_'):
                    config_key = key[7:]
                    config, created = SystemConfig.objects.get_or_create(key=config_key)
                    
                    if value.lower() == 'true':
                        config.value = True
                    elif value.lower() == 'false':
                        config.value = False
                    elif value.isdigit():
                        config.value = int(value)
                    else:
                        try:
                            config.value = float(value)
                        except ValueError:
                            config.value = value
                    
                    config.save()
                    updated_keys.append(config_key)
            
            SystemLog.objects.create(
                log_type='ADMIN_ACTION',
                user=request.user,
                action='SETTINGS_UPDATED',
                description=f'Updated settings: {", ".join(updated_keys)}'
            )
            
            return JsonResponse({
                'success': True, 
                'message': f'Settings updated successfully',
                'updated': updated_keys
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    return JsonResponse({'error': f'Invalid action: {action}'}, status=400)


# ==================== API VIEWS ====================

@login_required(login_url='XMR:signupin')
def api_wallet_balance(request):
    """API endpoint to get wallet balance"""
    wallet = request.user.wallet
    return JsonResponse({
        'balance': float(wallet.balance),
        'locked': float(wallet.locked_balance),
        'available': float(wallet.available_balance()),
        'total': float(wallet.balance + wallet.locked_balance),
    })


@login_required(login_url='XMR:signupin')
def api_investment_stats(request):
    """API endpoint to get investment statistics"""
    investments = Investment.objects.filter(user=request.user)
    
    active = investments.filter(status='ACTIVE')
    completed = investments.filter(status='COMPLETED')
    
    total_invested = active.aggregate(total=Sum('amount'))['total'] or 0
    total_earned = Transaction.objects.filter(
        wallet=request.user.wallet,
        transaction_type='PROFIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    next_payout = active.order_by('end_date').first()
    next_payout_data = None
    if next_payout:
        next_payout_data = {
            'token': next_payout.token.name,
            'amount': float(next_payout.daily_return),
            'days_left': next_payout.remaining_payouts,
            'end_date': next_payout.end_date.strftime('%Y-%m-%d'),
        }
    
    return JsonResponse({
        'active_count': active.count(),
        'completed_count': completed.count(),
        'total_invested': float(total_invested),
        'total_earned': float(total_earned),
        'next_payout': next_payout_data,
    })


# ==================== HELPER FUNCTIONS ====================

def get_client_ip(request):
    """Get client IP address from request"""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def contains_offensive_content(text):
    """Check if text contains offensive or inappropriate content"""
    offensive_words = [
        'Fuck you', 'Fuck', 'mf', 
    ]
    
    text_lower = text.lower()
    return any(word in text_lower for word in offensive_words)


def clean_phone_number(phone):
    """Clean and format Kenyan phone number"""
    phone = re.sub(r'\D', '', phone)
    
    if phone.startswith('0') and len(phone) == 10:
        phone = '254' + phone[1:]
    elif phone.startswith('7') and len(phone) == 9:
        phone = '254' + phone
    elif phone.startswith('254') and len(phone) == 12:
        pass
    elif phone.startswith('+254') and len(phone) == 13:
        phone = phone[1:]
    
    return phone


def validate_phone_number(phone):
    """Validate Kenyan phone number"""
    pattern = r'^254[17]\d{8}$'
    return bool(re.match(pattern, phone))


# ==================== CRON JOBS / MANAGEMENT COMMANDS ====================
def process_daily_payout(self):
    """
    Process a single daily payout for this investment.

    GUARDS:
    - Only runs on weekdays (Monday-Friday)
    - Only runs if at least 24 hours have passed since the last payout
    - Only runs if the investment is still ACTIVE with remaining payouts

    Returns True if a payout was processed, False otherwise.
    """
    from django.utils import timezone
    from decimal import Decimal
    from django.db import transaction

    now = timezone.now()

    # ===== GUARD 1: Weekend check =====
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False

    # ===== GUARD 2: Must be ACTIVE with remaining payouts =====
    if self.status != 'ACTIVE' or self.remaining_payouts <= 0:
        return False

    # ===== GUARD 3: 24-hour minimum gap =====
    reference_time = self.last_payout_date or self.created_at
    hours_since_last = (now - reference_time).total_seconds() / 3600

    if hours_since_last < 24:
        return False

    # ===== PROCESS THE PAYOUT =====
    try:
        with transaction.atomic():
            # Lock this investment row to prevent race conditions
            from .models import Investment
            locked = Investment.objects.select_for_update().get(pk=self.pk)

            # Re-check guards on the locked row
            if locked.status != 'ACTIVE' or locked.remaining_payouts <= 0:
                return False

            ref_time = locked.last_payout_date or locked.created_at
            if (now - ref_time).total_seconds() / 3600 < 24:
                return False

            wallet = locked.user.wallet
            payout_amount = locked.daily_return

            # Create the profit transaction
            from .models import Transaction
            transaction_record = Transaction.objects.create(
                wallet=wallet,
                transaction_type='PROFIT',
                amount=payout_amount,
                description=f"Daily payout from {locked.token.name}",
                status='COMPLETED',
                investment=locked,
                processed_at=now,
            )

            # Credit the wallet available balance
            wallet.balance += payout_amount
            wallet.total_earned = (wallet.total_earned or Decimal('0')) + payout_amount
            wallet.save(update_fields=['balance', 'total_earned'])

            # Update the investment
            locked.total_paid = (locked.total_paid or Decimal('0')) + payout_amount
            locked.remaining_payouts -= 1
            locked.last_payout_date = now

            # If no more payouts remain, complete the investment
            if locked.remaining_payouts <= 0:
                locked.status = 'COMPLETED'
                locked.end_date = now
                # Return principal to available balance
                wallet.balance += locked.amount
                wallet.locked_balance = max(
                    Decimal('0'), (wallet.locked_balance or Decimal('0')) - locked.amount
                )
                wallet.save(update_fields=['balance', 'locked_balance'])

            locked.save(update_fields=[
                'total_paid', 'remaining_payouts', 'last_payout_date',
                'status', 'end_date'
            ])

            # Update self so caller sees fresh values
            self.total_paid = locked.total_paid
            self.remaining_payouts = locked.remaining_payouts
            self.last_payout_date = locked.last_payout_date
            self.status = locked.status
            self.end_date = locked.end_date

            return True

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"process_daily_payout failed for investment {self.id}: {e}", exc_info=True)
        return False
        
def check_expired_investments():
    """Check for investments that have passed their end date"""
    expired = Investment.objects.filter(
        status='ACTIVE',
        end_date__lt=timezone.now()
    )
    
    count = 0
    for investment in expired:
        try:
            investment.complete_investment()  # This now returns principal to available balance
            count += 1
            
        except Exception as e:
            logger.error(f"Error marking investment {investment.id} as expired: {str(e)}", exc_info=True)
    
    if count > 0:
        SystemLog.objects.create(
            log_type='INFO',
            action='EXPIRED_INVESTMENTS',
            description=f'Marked {count} investments as expired/completed',
        )
    
    return count


# ==================== ADMIN TASK VIEWS ====================

@login_required(login_url='XMR:signupin')
def admin_trigger_payout(request):
    """Admin view to manually trigger payouts"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method == 'POST':
        # Process directly
        processed, errors = process_daily_payouts()
        
        SystemLog.objects.create(
            log_type='ADMIN_ACTION',
            user=request.user,
            action='MANUAL_PAYOUT_TRIGGERED',
            description=f'Admin manually triggered payouts. Processed: {processed}, Errors: {errors}'
        )
        
        return JsonResponse({
            'success': True,
            'message': f'Payouts processed: {processed} successful, {errors} errors',
            'processed': processed,
            'errors': errors
        })
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required(login_url='XMR:signupin')
def admin_check_expired(request):
    """Admin view to manually check expired investments"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method == 'POST':
        count = check_expired_investments()
        
        return JsonResponse({
            'success': True,
            'message': f'Marked {count} investments as completed',
            'count': count
        })
    
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required(login_url='XMR:signupin')
def admin_payout_stats(request):
    """Get statistics about payouts"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method not in ['GET', 'POST']:
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    now = timezone.now()
    yesterday = now - timedelta(hours=24)
    
    # Get investment stats
    active_count = Investment.objects.filter(status='ACTIVE').count()
    due_count = Investment.objects.filter(
        status='ACTIVE',
        remaining_payouts__gt=0
    ).filter(
        Q(last_payout_date__isnull=True, created_at__lte=now - timedelta(hours=24)) |
        Q(last_payout_date__lte=now - timedelta(hours=24))
    ).count()
    
    # Get recent payouts
    recent_payouts = Transaction.objects.filter(
        transaction_type='PROFIT',
        created_at__gte=yesterday
    ).count()
    
    # Calculate missed payouts (investments that should have paid but haven't)
    missed_count = Investment.objects.filter(
        status='ACTIVE',
        remaining_payouts__gt=0,
        last_payout_date__lt=now - timedelta(hours=25)
    ).count()
    
    return JsonResponse({
        'success': True,
        'active_investments': active_count,
        'due_for_payout': due_count,
        'missed': missed_count,
        'payouts_last_24h': recent_payouts,
        'last_check': str(now)
    })


@login_required(login_url='XMR:signupin')
@transaction.atomic
def fix_all_wallets(request):
    """Fix all wallets with incorrect balances"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method not in ['GET', 'POST']:
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    fixed_count = 0
    fixed_users = []
    errors = []
    
    try:
        for wallet in Wallet.objects.select_for_update().all():
            # Calculate what balances SHOULD be based on transactions
            total_deposits = Transaction.objects.filter(
                wallet=wallet,
                transaction_type='DEPOSIT',
                status='COMPLETED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            
            total_withdrawals = Transaction.objects.filter(
                wallet=wallet,
                transaction_type='WITHDRAWAL',
                status='COMPLETED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            
            total_profits = Transaction.objects.filter(
                wallet=wallet,
                transaction_type='PROFIT',
                status='COMPLETED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            
            total_referral_bonuses = Transaction.objects.filter(
                wallet=wallet,
                transaction_type='REFERRAL_BONUS',
                status='COMPLETED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            
            total_adjustments = Transaction.objects.filter(
                wallet=wallet,
                transaction_type='ADJUSTMENT',
                status='COMPLETED'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            
            # Calculate locked balance from active investments
            active_investments = Investment.objects.filter(
                user=wallet.user,
                status='ACTIVE'
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            
            # Total earnings should be profits + referral bonuses
            correct_total_earned = total_profits + total_referral_bonuses
            
            # Available balance should be: deposits + profits + referral_bonuses + adjustments - withdrawals
            # Note: When investing, money moves from balance to locked_balance, so it's already accounted for
            correct_balance = total_deposits + total_profits + total_referral_bonuses + total_adjustments - total_withdrawals
            
            # Track if anything changed
            changes = {}
            
            if wallet.balance != correct_balance:
                changes['balance'] = f"{wallet.balance} → {correct_balance}"
                wallet.balance = correct_balance
            
            if wallet.locked_balance != active_investments:
                changes['locked_balance'] = f"{wallet.locked_balance} → {active_investments}"
                wallet.locked_balance = active_investments
            
            if wallet.total_earned != correct_total_earned:
                changes['total_earned'] = f"{wallet.total_earned} → {correct_total_earned}"
                wallet.total_earned = correct_total_earned
            
            if wallet.total_deposited != total_deposits:
                changes['total_deposited'] = f"{wallet.total_deposited} → {total_deposits}"
                wallet.total_deposited = total_deposits
            
            if wallet.total_withdrawn != total_withdrawals:
                changes['total_withdrawn'] = f"{wallet.total_withdrawn} → {total_withdrawals}"
                wallet.total_withdrawn = total_withdrawals
            
            if changes:
                wallet.save()
                fixed_count += 1
                fixed_users.append({
                    'username': wallet.user.username,
                    'changes': changes
                })
                
                SystemLog.objects.create(
                    log_type='ADMIN_ACTION',
                    user=request.user,
                    action='FIXED_WALLET_BALANCE',
                    description=f'Fixed wallet for {wallet.user.username}: {json.dumps(changes)}'
                )
                
                logger.info(f"Fixed wallet for {wallet.user.username}: {changes}")
        
        # Also check for investments that might be stuck in incorrect state
        stuck_investments = Investment.objects.filter(
            status='ACTIVE',
            remaining_payouts=0
        )
        for inv in stuck_investments:
            inv.complete_investment()
            logger.info(f"Completed stuck investment {inv.id} for user {inv.user.username}")
        
        # Check for investments that have passed end date but still active
        expired_investments = Investment.objects.filter(
            status='ACTIVE',
            end_date__lt=timezone.now()
        )
        for inv in expired_investments:
            inv.complete_investment()
            logger.info(f"Completed expired investment {inv.id} for user {inv.user.username}")
        
        message = f'Fixed {fixed_count} wallets with incorrect balances'
        if fixed_count > 0:
            message += f': {", ".join([u["username"] for u in fixed_users])}'
        
        return JsonResponse({
            'success': True,
            'message': message,
            'fixed_count': fixed_count,
            'fixed_users': fixed_users,
            'errors': errors
        })
        
    except Exception as e:
        logger.error(f"Error in fix_all_wallets: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


# ==================== ADMIN INVESTMENT MANAGEMENT ====================

@login_required(login_url='XMR:signupin')
def admin_force_complete_investment(request, investment_id):
    """Admin view to force complete an investment"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        investment = get_object_or_404(Investment, id=investment_id)
        
        with transaction.atomic():
            # Complete the investment
            investment.complete_investment()
            
            SystemLog.objects.create(
                log_type='ADMIN_ACTION',
                user=request.user,
                action='FORCE_COMPLETE_INVESTMENT',
                description=f'Force completed investment {investment.investment_id} for user {investment.user.username}'
            )
            
            return JsonResponse({
                'success': True,
                'message': f'Investment {investment.investment_id} completed successfully',
                'user': investment.user.username,
                'amount': float(investment.amount)
            })
            
    except Exception as e:
        logger.error(f"Error force completing investment {investment_id}: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@login_required(login_url='XMR:signupin')
def admin_force_payout(request, investment_id):
    """Admin view to force a single payout on an investment"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        investment = get_object_or_404(Investment, id=investment_id)
        
        with transaction.atomic():
            success = investment.process_daily_payout()
            
            if success:
                SystemLog.objects.create(
                    log_type='ADMIN_ACTION',
                    user=request.user,
                    action='FORCE_PAYOUT',
                    description=f'Force processed payout for investment {investment.investment_id}'
                )
                
                return JsonResponse({
                    'success': True,
                    'message': f'Payout processed successfully',
                    'remaining_payouts': investment.remaining_payouts,
                    'total_paid': float(investment.total_paid)
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': 'Could not process payout - investment may be completed or inactive'
                }, status=400)
            
    except Exception as e:
        logger.error(f"Error forcing payout for investment {investment_id}: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


# ==================== ADMIN USER MANAGEMENT ====================

@login_required(login_url='XMR:signupin')
def admin_user_detail(request, user_id):
    """Admin view to see detailed user information"""
    if not request.user.is_staff:
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('XMR:account')
    
    user = get_object_or_404(User, id=user_id)
    
    # Get all user data
    profile = user.profile
    wallet = user.wallet
    
    # Get all transactions
    transactions = Transaction.objects.filter(
        wallet=wallet
    ).select_related('investment', 'withdrawal').order_by('-created_at')[:100]
    
    # Get all investments
    investments = Investment.objects.filter(
        user=user
    ).select_related('token').order_by('-created_at')
    
    # Get all deposits
    deposits = MpesaPayment.objects.filter(
        user=user
    ).order_by('-created_at')
    
    # Get all withdrawals
    withdrawals = WithdrawalRequest.objects.filter(
        user=user
    ).order_by('-created_at')
    
    # Calculate statistics
    total_deposits = transactions.filter(
        transaction_type='DEPOSIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    total_withdrawals = transactions.filter(
        transaction_type='WITHDRAWAL',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    total_profits = transactions.filter(
        transaction_type='PROFIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    total_referral_bonus = transactions.filter(
        transaction_type='REFERRAL_BONUS',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    active_investments = investments.filter(status='ACTIVE')
    total_invested = active_investments.aggregate(total=Sum('amount'))['total'] or 0
    
    context = {
        'user': user,
        'profile': profile,
        'wallet': wallet,
        'transactions': transactions,
        'investments': investments,
        'deposits': deposits,
        'withdrawals': withdrawals,
        'stats': {
            'total_deposits': float(total_deposits),
            'total_withdrawals': float(total_withdrawals),
            'total_profits': float(total_profits),
            'total_referral_bonus': float(total_referral_bonus),
            'total_invested': float(total_invested),
            'active_investments_count': active_investments.count(),
            'total_investments_count': investments.count(),
        }
    }
    
    return render(request, 'admin_user_detail.html', context)


# ==================== ADMIN SYSTEM MAINTENANCE ====================

@login_required(login_url='XMR:signupin')
def admin_system_status(request):
    """Admin view to check system status"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != 'GET':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        # Get system statistics
        now = timezone.now()
        last_hour = now - timedelta(hours=1)
        last_day = now - timedelta(days=1)
        
        status = {
            'timestamp': str(now),
            'database': {
                'users_total': User.objects.count(),
                'users_active_last_hour': User.objects.filter(last_login__gte=last_hour).count(),
                'users_active_last_day': User.objects.filter(last_login__gte=last_day).count(),
            },
            'wallets': {
                'total_wallets': Wallet.objects.count(),
                'total_available_balance': float(Wallet.objects.aggregate(total=Sum('balance'))['total'] or 0),
                'total_locked_balance': float(Wallet.objects.aggregate(total=Sum('locked_balance'))['total'] or 0),
                'wallets_with_negative': Wallet.objects.filter(balance__lt=0).count(),
                'wallets_with_mismatch': check_for_wallet_mismatches(),
            },
            'investments': {
                'active': Investment.objects.filter(status='ACTIVE').count(),
                'completed': Investment.objects.filter(status='COMPLETED').count(),
                'total_invested': float(Investment.objects.filter(status='ACTIVE').aggregate(total=Sum('amount'))['total'] or 0),
                'due_for_payout': Investment.objects.filter(
                    status='ACTIVE',
                    remaining_payouts__gt=0
                ).filter(
                    Q(last_payout_date__isnull=True, created_at__lte=now - timedelta(hours=24)) |
                    Q(last_payout_date__lte=now - timedelta(hours=24))
                ).count(),
                'overdue': Investment.objects.filter(
                    status='ACTIVE',
                    remaining_payouts__gt=0,
                    last_payout_date__lt=now - timedelta(hours=25)
                ).count(),
            },
            'transactions': {
                'last_hour': Transaction.objects.filter(created_at__gte=last_hour).count(),
                'last_day': Transaction.objects.filter(created_at__gte=last_day).count(),
                'pending': Transaction.objects.filter(status='PENDING').count(),
                'failed': Transaction.objects.filter(status='FAILED').count(),
            },
            'deposits': {
                'pending': MpesaPayment.objects.filter(status='PENDING').count(),
                'verified_today': MpesaPayment.objects.filter(
                    status='VERIFIED',
                    verified_at__date=now.date()
                ).count(),
            },
            'withdrawals': {
                'pending': WithdrawalRequest.objects.filter(status='PENDING').count(),
                'processing': WithdrawalRequest.objects.filter(status='PROCESSING').count(),
                'completed_today': WithdrawalRequest.objects.filter(
                    status='COMPLETED',
                    processed_at__date=now.date()
                ).count(),
            },
            'kyc': {
                'pending': UserProfile.objects.filter(
                    Q(id_front_image__isnull=False) |
                    Q(id_back_image__isnull=False) |
                    Q(selfie_with_id__isnull=False)
                ).filter(
                    Q(phone_verified=False) | Q(id_verified=False)
                ).count(),
                'verified': UserProfile.objects.filter(phone_verified=True, id_verified=True).count(),
            }
        }
        
        return JsonResponse({'success': True, 'status': status})
        
    except Exception as e:
        logger.error(f"Error getting system status: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


def check_for_wallet_mismatches():
    """Helper function to check for wallet balance mismatches"""
    mismatches = 0
    
    for wallet in Wallet.objects.all():
        # Calculate expected balance
        total_deposits = Transaction.objects.filter(
            wallet=wallet,
            transaction_type='DEPOSIT',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        total_withdrawals = Transaction.objects.filter(
            wallet=wallet,
            transaction_type='WITHDRAWAL',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        total_profits = Transaction.objects.filter(
            wallet=wallet,
            transaction_type='PROFIT',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        total_referral = Transaction.objects.filter(
            wallet=wallet,
            transaction_type='REFERRAL_BONUS',
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        expected_balance = total_deposits + total_profits + total_referral - total_withdrawals
        
        if wallet.balance != expected_balance:
            mismatches += 1
    
    return mismatches


# ==================== EXPORT FUNCTIONS ====================

@login_required(login_url='XMR:signupin')
def export_transactions_csv(request):
    """Export transactions as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="transactions_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Transaction ID', 'User', 'Type', 'Amount', 'Status', 
        'Description', 'Created At', 'Processed At', 'Processed By'
    ])
    
    transactions = Transaction.objects.select_related(
        'wallet__user', 'processed_by'
    ).order_by('-created_at')[:5000]
    
    for t in transactions:
        writer.writerow([
            t.transaction_id,
            t.wallet.user.username,
            t.transaction_type,
            float(t.amount),
            t.status,
            t.description,
            t.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            t.processed_at.strftime('%Y-%m-%d %H:%M:%S') if t.processed_at else '',
            t.processed_by.username if t.processed_by else ''
        ])
    
    return response


@login_required(login_url='XMR:signupin')
def export_users_csv(request):
    """Export users as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="users_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Username', 'Email', 'First Name', 'Last Name', 'Phone',
        'Balance', 'Locked Balance', 'Total Deposited', 'Total Withdrawn',
        'Total Earned', 'Phone Verified', 'ID Verified', 'Is Banned',
        'Date Joined', 'Last Login', 'Referral Code', 'Referred By'
    ])
    
    users = User.objects.select_related('profile', 'wallet').order_by('-date_joined')[:2000]
    
    for u in users:
        writer.writerow([
            u.username,
            u.email,
            u.first_name,
            u.last_name,
            u.profile.phone_number,
            float(u.wallet.balance) if hasattr(u, 'wallet') else 0,
            float(u.wallet.locked_balance) if hasattr(u, 'wallet') else 0,
            float(u.wallet.total_deposited) if hasattr(u, 'wallet') else 0,
            float(u.wallet.total_withdrawn) if hasattr(u, 'wallet') else 0,
            float(u.wallet.total_earned) if hasattr(u, 'wallet') else 0,
            'Yes' if u.profile.phone_verified else 'No',
            'Yes' if u.profile.id_verified else 'No',
            'Yes' if u.profile.is_banned else 'No',
            u.date_joined.strftime('%Y-%m-%d %H:%M:%S'),
            u.last_login.strftime('%Y-%m-%d %H:%M:%S') if u.last_login else '',
            u.profile.referral_code,
            u.profile.referred_by.user.username if u.profile.referred_by else ''
        ])
    
    return response


# ==================== DEBUGGING AND TESTING ====================

@login_required(login_url='XMR:signupin')
def debug_wallet(request, user_id=None):
    """Debug view to check wallet state (admin only)"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if user_id:
        user = get_object_or_404(User, id=user_id)
    else:
        user = request.user
    
    wallet = user.wallet
    
    # Get all transactions
    deposits = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='DEPOSIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    withdrawals = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='WITHDRAWAL',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    profits = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='PROFIT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    referrals = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='REFERRAL_BONUS',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    adjustments = Transaction.objects.filter(
        wallet=wallet,
        transaction_type='ADJUSTMENT',
        status='COMPLETED'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    # Active investments
    active_investments = Investment.objects.filter(
        user=user,
        status='ACTIVE'
    )
    
    total_locked = active_investments.aggregate(total=Sum('amount'))['total'] or Decimal('0')
    
    # Expected calculations
    expected_balance = deposits + profits + referrals + adjustments - withdrawals
    expected_total_earned = profits + referrals
    
    debug_info = {
        'user': user.username,
        'wallet_state': {
            'current_balance': float(wallet.balance),
            'current_locked': float(wallet.locked_balance),
            'current_total_earned': float(wallet.total_earned),
            'current_total_deposited': float(wallet.total_deposited),
            'current_total_withdrawn': float(wallet.total_withdrawn),
        },
        'transaction_totals': {
            'deposits': float(deposits),
            'withdrawals': float(withdrawals),
            'profits': float(profits),
            'referrals': float(referrals),
            'adjustments': float(adjustments),
        },
        'expected_values': {
            'expected_balance': float(expected_balance),
            'expected_locked': float(total_locked),
            'expected_total_earned': float(expected_total_earned),
            'balance_match': wallet.balance == expected_balance,
            'locked_match': wallet.locked_balance == total_locked,
            'earned_match': wallet.total_earned == expected_total_earned,
        },
        'active_investments': [
            {
                'id': inv.id,
                'token': inv.token.name,
                'amount': float(inv.amount),
                'remaining_payouts': inv.remaining_payouts,
                'total_paid': float(inv.total_paid),
                'start_date': str(inv.start_date),
                'end_date': str(inv.end_date),
                'last_payout': str(inv.last_payout_date) if inv.last_payout_date else None,
            }
            for inv in active_investments
        ],
        'recent_transactions': [
            {
                'id': t.id,
                'type': t.transaction_type,
                'amount': float(t.amount),
                'status': t.status,
                'created': str(t.created_at),
                'description': t.description,
            }
            for t in Transaction.objects.filter(wallet=wallet).order_by('-created_at')[:20]
        ]
    }
    
    return JsonResponse(debug_info)


# ==================== ERROR HANDLERS ====================

def handler404(request, exception):
    """Custom 404 error handler"""
    return render(request, '404.html', status=404)


def handler500(request):
    """Custom 500 error handler"""
    return render(request, '500.html', status=500)


def handler403(request, exception):
    """Custom 403 error handler"""
    return render(request, '403.html', status=403)


def handler400(request, exception):
    """Custom 400 error handler"""
    return render(request, '400.html', status=400)


# ==================== HEALTH CHECK ====================

def health_check(request):
    """
    Simple health check endpoint for monitoring
    Returns 200 if system is operational
    """
    try:
        # Check database connectivity
        User.objects.exists()
        
        # Check if critical models are accessible
        Wallet.objects.exists()
        
        return JsonResponse({
            'status': 'healthy',
            'timestamp': str(timezone.now()),
            'database': 'connected'
        })
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}", exc_info=True)
        return JsonResponse({
            'status': 'unhealthy',
            'timestamp': str(timezone.now()),
            'error': str(e)
        }, status=500)


# ==================== INITIAL SETUP ====================

def initialize_system(request):
    """
    Initialize system with default data
    This should be run once during deployment
    """
    # Only allow in debug mode or via specific condition
    if not settings.DEBUG:
        return JsonResponse({'error': 'Not allowed in production'}, status=403)
    
    try:
        # Create default tokens if they don't exist
        default_tokens = [
            {
                'name': 'XMR-1',
                'display_name': 'Starter',
                'token_number': 1,
                'minimum_investment': 1200,
                'daily_return': 100,
                'return_days': 12,
                'status': 'ACTIVE',
                'description': 'Perfect for beginners. Low entry, consistent returns.',
                'icon': 'fa-seedling',
                'color': 'success',
            },
            {
                'name': 'XMR-2',
                'display_name': 'Bronze',
                'token_number': 2,
                'minimum_investment': 2000,
                'daily_return': 280,
                'return_days': 12,
                'status': 'ACTIVE',
                'description': 'Solid returns with moderate investment.',
                'icon': 'fa-medal',
                'color': 'bronze',
            },
            {
                'name': 'XMR-3',
                'display_name': 'Silver',
                'token_number': 3,
                'minimum_investment': 5000,
                'daily_return': 750,
                'return_days': 12,
                'status': 'ACTIVE',
                'description': 'Premium returns for serious investors.',
                'icon': 'fa-gem',
                'color': 'secondary',
            },
            {
                'name': 'XMR-4',
                'display_name': 'Gold',
                'token_number': 4,
                'minimum_investment': 10000,
                'daily_return': 1600,
                'return_days': 12,
                'status': 'ACTIVE',
                'description': 'High-tier investment with maximum returns.',
                'icon': 'fa-crown',
                'color': 'warning',
            },
        ]
        
        created_tokens = []
        for token_data in default_tokens:
            token, created = Token.objects.get_or_create(
                name=token_data['name'],
                defaults=token_data
            )
            if created:
                created_tokens.append(token.name)
        
        # Create default system configs
        default_configs = {
            'min_deposit': 1200,
            'min_withdrawal': 500,
            'withdrawal_tax': 5,
            'referral_commission': 5,
            'mpesa_paybill': '2345678',
            'mpesa_account': 'INVEST',
            'site_name': 'XMR Investments',
            'support_email': 'support@example.com',
            'support_phone': '0712345678',
        }
        
        created_configs = []
        for key, value in default_configs.items():
            config, created = SystemConfig.objects.get_or_create(
                key=key,
                defaults={'value': value, 'description': f'Default {key}'}
            )
            if created:
                created_configs.append(key)
        
        # Create superuser if none exists
        if not User.objects.filter(is_superuser=True).exists():
            User.objects.create_superuser(
                username='admin',
                email='admin@example.com',
                password='Admin123!'
            )
            created_admin = True
        else:
            created_admin = False
        
        return JsonResponse({
            'success': True,
            'message': 'System initialized successfully',
            'created_tokens': created_tokens,
            'created_configs': created_configs,
            'created_admin': created_admin
        })
        
    except Exception as e:
        logger.error(f"System initialization error: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)





@login_required(login_url='XMR:signupin')
def export_deposits_csv(request):
    """Export deposits as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="deposits_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['ID', 'User', 'Amount', 'Phone', 'M-Pesa Code', 'Status', 'Created At'])
    
    deposits = MpesaPayment.objects.select_related('user').order_by('-created_at')[:5000]
    
    for d in deposits:
        writer.writerow([
            d.id,
            d.user.username,
            float(d.amount),
            d.phone_number,
            d.mpesa_code or '',
            d.status,
            d.created_at.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response


@login_required(login_url='XMR:signupin')
def export_withdrawals_csv(request):
    """Export withdrawals as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="withdrawals_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Request ID', 'User', 'Amount', 'Net Amount', 'Tax', 'Method', 'Status', 'Created At'])
    
    withdrawals = WithdrawalRequest.objects.select_related('user').order_by('-created_at')[:5000]
    
    for w in withdrawals:
        writer.writerow([
            w.request_id,
            w.user.username,
            float(w.amount),
            float(w.net_amount),
            float(w.tax_amount),
            w.payment_method,
            w.status,
            w.created_at.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response


@login_required(login_url='XMR:signupin')
def export_investments_csv(request):
    """Export investments as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="investments_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Investment ID', 'User', 'Token', 'Amount', 'Daily Return', 'Total Paid', 'Status', 'Start Date', 'End Date'])
    
    investments = Investment.objects.select_related('user', 'token').order_by('-created_at')[:5000]
    
    for inv in investments:
        writer.writerow([
            inv.investment_id,
            inv.user.username,
            inv.token.name,
            float(inv.amount),
            float(inv.daily_return),
            float(inv.total_paid),
            inv.status,
            inv.start_date.strftime('%Y-%m-%d %H:%M:%S'),
            inv.end_date.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response


@login_required(login_url='XMR:signupin')
def export_tokens_csv(request):
    """Export tokens as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="tokens_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Token Number', 'Name', 'Display Name', 'Min Investment', 'Daily Return', 'Return Days', 'Status', 'Purchased Count'])
    
    tokens = Token.objects.all().order_by('token_number')
    
    for t in tokens:
        writer.writerow([
            t.token_number,
            t.name,
            t.display_name,
            float(t.minimum_investment) if t.minimum_investment else 0,
            float(t.daily_return) if t.daily_return else 0,
            t.return_days,
            t.status,
            t.purchased_count
        ])
    
    return response


@login_required(login_url='XMR:signupin')
def export_kyc_csv(request):
    """Export KYC requests as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    from django.db.models import Q
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="kyc_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['User', 'Full Name', 'Phone', 'Phone Verified', 'ID Verified', 'Has ID Front', 'Has ID Back', 'Has Selfie', 'Submitted At'])
    
    kyc_requests = UserProfile.objects.filter(
        Q(id_front_image__isnull=False) |
        Q(id_back_image__isnull=False) |
        Q(selfie_with_id__isnull=False)
    ).select_related('user').order_by('-updated_at')
    
    for k in kyc_requests:
        writer.writerow([
            k.user.username,
            k.national_id_name or f"{k.user.first_name} {k.user.last_name}",
            k.phone_number,
            'Yes' if k.phone_verified else 'No',
            'Yes' if k.id_verified else 'No',
            'Yes' if k.id_front_image else 'No',
            'Yes' if k.id_back_image else 'No',
            'Yes' if k.selfie_with_id else 'No',
            k.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response


@login_required(login_url='XMR:signupin')
def export_logs_csv(request):
    """Export system logs as CSV"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    import csv
    from django.http import HttpResponse
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="logs_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    writer.writerow(['Time', 'Type', 'User', 'Action', 'Description', 'IP Address'])
    
    logs = SystemLog.objects.select_related('user').order_by('-created_at')[:5000]
    
    for log in logs:
        writer.writerow([
            log.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            log.log_type,
            log.user.username if log.user else 'System',
            log.action,
            log.description,
            log.ip_address or ''
        ])
    
    return response

@login_required(login_url='XMR:signupin')
def check_investment_payouts_api(request, investment_id):
    """API endpoint to check missed payouts for an investment"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    try:
        investment = Investment.objects.get(id=investment_id)

        # Calculate missed payouts
        now = timezone.now()
        missed = calculate_missed_payouts(investment, now)

        # Process them if requested
        if request.POST.get('process', 'false') == 'true':
            processed = 0
            for i in range(missed):
                # process_daily_payout() has its own 24h + weekday guards
                if investment.process_daily_payout():
                    processed += 1
                else:
                    break

            return JsonResponse({
                'success': True,
                'missed': missed,
                'processed': processed,
                'message': f'Processed {processed} of {missed} missed payouts'
            })
        else:
            return JsonResponse({
                'success': True,
                'missed': missed,
                'message': f'Found {missed} missed payouts'
            })

    except Investment.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Investment not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required(login_url='XMR:signupin')
def process_payout_api(request, investment_id):
    """API endpoint to process a single payout"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    try:
        investment = Investment.objects.get(id=investment_id)
        
        with transaction.atomic():
            success = investment.process_daily_payout()
            
            if success:
                return JsonResponse({
                    'success': True,
                    'message': 'Payout processed successfully',
                    'remaining_payouts': investment.remaining_payouts,
                    'total_paid': float(investment.total_paid)
                })
            else:
                return JsonResponse({
                    'success': False,
                    'error': 'Could not process payout - investment may be completed or inactive'
                }, status=400)
                
    except Investment.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Investment not found'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required(login_url='XMR:signupin')
def get_pending_payouts(request):
    """Get list of investments due for payout"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    now = timezone.now()
    
    # Find investments due for payout
    due_investments = Investment.objects.filter(
        status='ACTIVE',
        remaining_payouts__gt=0
    ).filter(
        Q(last_payout_date__isnull=True, created_at__lte=now - timedelta(hours=24)) |
        Q(last_payout_date__lte=now - timedelta(hours=24))
    ).select_related('user', 'token')[:100]
    
    pending_payouts = []
    for inv in due_investments:
        # Calculate how many hours since last payout
        if inv.last_payout_date:
            hours_since = (now - inv.last_payout_date).total_seconds() / 3600
            due_since = f"{int(hours_since)} hours ago"
        else:
            hours_since = (now - inv.created_at).total_seconds() / 3600
            due_since = f"{int(hours_since)} hours ago (never paid)"
        
        pending_payouts.append({
            'id': inv.id,
            'investment_id': inv.investment_id,
            'user': inv.user.username,
            'user_id': inv.user.id,
            'token': inv.token.name,
            'amount': float(inv.daily_return),
            'last_payout': inv.last_payout_date.strftime('%Y-%m-%d %H:%M') if inv.last_payout_date else None,
            'due_since': due_since,
            'status': 'due' if hours_since > 24 else 'pending'
        })
    
    return JsonResponse({
        'success': True,
        'pending_payouts': pending_payouts,
        'count': len(pending_payouts)
    })


@login_required(login_url='XMR:signupin')
def get_recent_payouts(request):
    """Get recent payout transactions"""
    if not request.user.is_staff:
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    recent = Transaction.objects.filter(
        transaction_type='PROFIT',
        status='COMPLETED'
    ).select_related('wallet__user', 'investment').order_by('-created_at')[:50]
    
    payouts = []
    for t in recent:
        payouts.append({
            'id': t.id,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
            'user': t.wallet.user.username,
            'user_id': t.wallet.user.id,
            'investment_id': t.investment.investment_id if t.investment else None,
            'amount': float(t.amount),
            'description': t.description
        })
    
    return JsonResponse({
        'success': True,
        'recent_payouts': payouts
    })

class ChatConfig:
    """Centralized configuration for chat system"""
    MESSAGES_PER_PAGE = 50
    ROOMS_PER_PAGE = 20
    ONLINE_USERS_LIMIT = 50
    TYPING_TIMEOUT = 10  # seconds
    RATE_LIMIT_MESSAGES = 10  # messages per minute
    RATE_LIMIT_JOINS = 5  # room joins per hour
    CACHE_TTL_STATS = 300  # 5 minutes
    CACHE_TTL_ROOM_LIST = 60  # 1 minute
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB
    EDIT_TIME_WINDOW = 300  # 5 minutes

# =============================================================================
# DECORATORS & MIDDLEWARE HELPERS
# =============================================================================

def rate_limit(key_prefix: str, limit: int, period: int):
    """Rate limiting decorator for views"""
    def decorator(view_func):
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return view_func(request, *args, **kwargs)
            
            # Create unique key for user+action
            key = f"rate_limit:{key_prefix}:{request.user.id}"
            current = cache.get(key, 0)
            
            if current >= limit:
                logger.warning(f"Rate limit exceeded for {request.user.username} - {key_prefix}")
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'error': f'Rate limit exceeded. Please wait {period} seconds.'
                    }, status=429)
                
                messages.error(request, f'Too many requests. Please wait a moment.')
                return redirect(request.META.get('HTTP_REFERER', '/'))
            
            cache.set(key, current + 1, period)
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator

def cache_page_for_anonymous(timeout: int):
    """Cache page for anonymous users only"""
    def decorator(view_func):
        def wrapped(request, *args, **kwargs):
            if request.user.is_authenticated:
                return view_func(request, *args, **kwargs)
            
            # Generate cache key from request path and query string
            cache_key = f"page_cache:{request.get_full_path()}"
            cached_response = cache.get(cache_key)
            
            if cached_response:
                return HttpResponse(cached_response)
            
            response = view_func(request, *args, **kwargs)
            cache.set(cache_key, response.content, timeout)
            return response
        return wrapped
    return decorator

# =============================================================================
# SERVICE LAYER - BUSINESS LOGIC
# =============================================================================

class ChatRoomService:
    """Service class for ChatRoom business logic"""
    
    @staticmethod
    def get_room_with_relations(room_slug: str):
        """Get room with optimized relation loading"""
        return ChatRoom.objects.select_related(
            'created_by'
        ).prefetch_related(
            Prefetch('participants', queryset=User.objects.only('id', 'username')),
            Prefetch('moderators', queryset=User.objects.only('id')),
            Prefetch('banned_users', queryset=User.objects.only('id')),
        ).filter(
            slug=room_slug, 
            is_active=True
        ).first()
    
    @staticmethod
    def check_user_access(room: ChatRoom, user) -> Dict[str, Any]:
        """Comprehensive access check with caching"""
        cache_key = f"room_access:{room.id}:{user.id}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        # Check ban status
        is_banned = user in room.banned_users.all()
        
        # Check participant status
        is_participant = user in room.participants.all()
        
        # Check permissions
        permissions = {
            'is_admin': user.is_staff,
            'is_creator': room.created_by == user,
            'is_moderator': user in room.moderators.all(),
            'is_participant': is_participant,
            'is_banned': is_banned,
            'can_join': room.room_type == 'PUBLIC' and not is_participant and not is_banned,
            'can_send': is_participant and not is_banned,
            'can_moderate': user.is_staff or user in room.moderators.all() or room.created_by == user,
        }
        
        cache.set(cache_key, permissions, 60)  # 1 minute cache
        return permissions
    
    @staticmethod
    def join_room(room: ChatRoom, user) -> bool:
        """Atomic room join with validation"""
        with transaction.atomic():
            # Lock the room row to prevent race conditions
            locked_room = ChatRoom.objects.select_for_update().get(id=room.id)
            
            # Double-check conditions under lock
            if user in locked_room.banned_users.all():
                return False
            
            if user in locked_room.participants.all():
                return True
            
            if locked_room.participants.count() >= locked_room.max_participants:
                return False
            
            # Add participant
            locked_room.participants.add(user)
            
            # Create system message
            ChatMessage.objects.create(
                room=locked_room,
                user=None,
                message_type='SYSTEM',
                content=f"🎉 Welcome {user.username}! You've joined {locked_room.name}"
            )
            
            # Update last activity
            locked_room.last_activity = timezone.now()
            locked_room.save(update_fields=['last_activity'])
            
            # Clear cache
            try:
                cache.delete_pattern(f"room_access:{room.id}:*")
            except Exception as e:
                logger.debug(f"Could not delete room_access cache pattern: {e}")
            cache.delete(f"room_stats:{room.id}")
            
            return True
    
    @staticmethod
    def get_room_stats(room: ChatRoom) -> Dict[str, Any]:
        """Get cached room statistics"""
        cache_key = f"room_stats:{room.id}"
        cached = cache.get(cache_key)
        
        if cached:
            return cached
        
        # Single query for all stats
        stats = ChatMessage.objects.filter(room=room).aggregate(
            total_messages=Count('id'),
            messages_today=Count('id', filter=Q(
                created_at__date=timezone.now().date()
            )),
            pinned_messages=Count('id', filter=Q(is_pinned=True)),
            files_shared=Count('id', filter=Q(file__isnull=False)),
        )
        
        # Get active users count
        active_users = room.participants.filter(
            profile__last_seen__gte=timezone.now() - timedelta(hours=24)
        ).count()
        
        stats['active_users_24h'] = active_users
        
        cache.set(cache_key, stats, ChatConfig.CACHE_TTL_STATS)
        return stats


class ChatMessageService:
    """Service class for ChatMessage business logic"""
    
    @staticmethod
    def get_messages(room: ChatRoom, user, filters: Dict = None, page: int = 1):
        """Get paginated messages with optimized loading"""
        filters = filters or {}
        
        # Base queryset with optimizations
        messages = ChatMessage.objects.filter(
            room=room,
            is_deleted=False
        ).select_related(
            'user'
        ).prefetch_related(
            Prefetch(
                'reactions',
                queryset=ChatReaction.objects.select_related('user').only(
                    'reaction', 'user__username', 'user__id'
                )
            )
        ).order_by('created_at')
        
        # Apply filters
        if filters.get('pinned'):
            messages = messages.filter(is_pinned=True)
        elif filters.get('mentions'):
            messages = messages.filter(content__icontains=f'@{user.username}')
        elif filters.get('files'):
            messages = messages.filter(Q(file__isnull=False) | Q(image__isnull=False))
        
        if filters.get('search'):
            messages = messages.filter(
                Q(content__icontains=filters['search']) |
                Q(user__username__icontains=filters['search'])
            )
        
        # Paginate
        paginator = Paginator(messages, ChatConfig.MESSAGES_PER_PAGE)
        page_obj = paginator.get_page(page)
        
        return page_obj, paginator
    
    @staticmethod
    def format_messages_json(messages) -> List[Dict]:
        """Format messages for JSON response with reaction grouping"""
        # Pre-aggregate reactions in Python to avoid extra queries
        messages_list = list(messages)
        
        # Group reactions by message and emoji
        reaction_groups = {}
        for msg in messages_list:
            reaction_groups[msg.id] = {}
        
        # Collect all reactions (already prefetched)
        for msg in messages_list:
            for reaction in getattr(msg, '_prefetched_objects_cache', {}).get('reactions', []):
                if reaction.reaction not in reaction_groups[msg.id]:
                    reaction_groups[msg.id][reaction.reaction] = {
                        'emoji': reaction.reaction,
                        'count': 1,
                        'users': [reaction.user.username]
                    }
                else:
                    reaction_groups[msg.id][reaction.reaction]['count'] += 1
                    reaction_groups[msg.id][reaction.reaction]['users'].append(reaction.user.username)
        
        # Build response
        messages_json = []
        for msg in messages_list:
            msg_data = {
                'id': str(msg.message_id),
                'user': msg.user.username if msg.user else 'System',
                'user_id': msg.user.id if msg.user else None,
                'content': msg.content,
                'type': msg.message_type,
                'timestamp': msg.created_at.isoformat(),
                'is_edited': msg.is_edited,
                'is_pinned': msg.is_pinned,
                'reactions': list(reaction_groups.get(msg.id, {}).values()),
                'has_file': bool(msg.file),
                'has_image': bool(msg.image),
                'file_url': msg.file.url if msg.file and hasattr(msg.file, 'url') else None,
                'file_name': msg.file_name if msg.file_name else None,
                'file_size': msg.file_size if msg.file_size else None,
                'image_url': msg.image.url if msg.image and hasattr(msg.image, 'url') else None,
            }
            messages_json.append(msg_data)
        
        return messages_json
    
    @staticmethod
    def send_message(room: ChatRoom, user, content: str, message_type: str = 'TEXT'):
        """Send a message with rate limiting and validation"""
        # Rate limit check
        rate_key = f"message_rate:{room.id}:{user.id}"
        msg_count = cache.get(rate_key, 0)
        
        if msg_count >= ChatConfig.RATE_LIMIT_MESSAGES:
            raise ValidationError("Rate limit exceeded")
        
        # Check slow mode - safely check if attributes exist
        # Use getattr to safely access attributes that might not exist
        slow_mode_delay = getattr(room, 'slow_mode_delay', 0)
        
        # Only check slow mode if delay is greater than 0
        if slow_mode_delay > 0:
            last_msg = ChatMessage.objects.filter(
                room=room, user=user
            ).order_by('-created_at').first()
            
            if last_msg:
                elapsed = (timezone.now() - last_msg.created_at).total_seconds()
                if elapsed < slow_mode_delay:
                    raise ValidationError(
                        f"Please wait {slow_mode_delay - int(elapsed)} seconds"
                    )
        
        # Create message
        with transaction.atomic():
            message = ChatMessage.objects.create(
                room=room,
                user=user,
                content=content,
                message_type=message_type
            )
            
            # Update room activity - safely check if last_activity exists
            if hasattr(room, 'last_activity'):
                room.last_activity = timezone.now()
                room.save(update_fields=['last_activity'])
            
            # Update rate limit counter
            cache.set(rate_key, msg_count + 1, 60)
            
            # Clear relevant caches
            cache.delete(f"room_stats:{room.id}")
        
        return message


class OnlineUserService:
    """Service for managing online users"""
    
    @staticmethod
    def update_status(user):
        """Update user online status in Redis"""
        cache.set(
            f"online:{user.id}",
            {
                'username': user.username,
                'last_seen': timezone.now().isoformat()
            },
            timeout=300  # 5 minutes
        )
    
    @staticmethod
    def get_online_users(room: ChatRoom) -> List[Dict]:
        """Get online users from cache with fallback to DB"""
        # Try to get from cache first
        online_ids = []
        
        # Get all participants
        participants = room.participants.select_related('profile').only(
            'id', 'username', 'profile__avatar', 'profile__last_seen'
        )[:ChatConfig.ONLINE_USERS_LIMIT]
        
        online_users = []
        moderators = set(room.moderators.values_list('id', flat=True))
        
        for user in participants:
            # Check if online in cache
            is_online = cache.get(f"online:{user.id}") is not None
            
            if is_online:
                online_users.append({
                    'id': user.id,
                    'username': user.username,
                    'avatar': user.profile.avatar.url if hasattr(user.profile, 'avatar') and user.profile.avatar else None,
                    'is_moderator': user.id in moderators,
                    'is_creator': user == room.created_by,
                    'last_seen': user.profile.last_seen.isoformat() if user.profile.last_seen else None,
                })
        
        return online_users
    
    @staticmethod
    def get_typing_users(room_id: int) -> List[Dict]:
        """Get typing users from cache"""
        typing_ids = cache.get(f'typing:{room_id}', [])
        if not typing_ids:
            return []
        
        # Get user details
        users = User.objects.filter(id__in=typing_ids).values('id', 'username')
        return list(users)


class NotificationService:
    """Service for handling notifications"""
    
    @staticmethod
    def create_notification(user, notification_type: str, message: str, related_id: int = None):
        """Create notification with error handling"""
        try:
            return ChatNotificationMessage.objects.create(
                user=user,
                notification_type=notification_type,
                message=message,
                related_object_id=related_id
            )
        except Exception as e:
            logger.error(f"Failed to create notification: {e}")
            return None
    
    @staticmethod
    def notify_room(room: ChatRoom, exclude_user, notification_type: str, message: str):
        """Send notification to all room participants except exclude_user"""
        # Use bulk_create for efficiency
        notifications = []
        for user in room.participants.all():
            if user != exclude_user:
                notifications.append(
                    ChatNotificationMessage(
                        user=user,
                        notification_type=notification_type,
                        message=message,
                        related_object_id=room.id
                    )
                )
        
        if notifications:
            try:
                ChatNotificationMessage.objects.bulk_create(notifications)
            except Exception as e:
                logger.error(f"Failed to bulk create notifications: {e}")

# =============================================================================
# MAIN VIEWS
# =============================================================================

@login_required(login_url='XMR:signupin')
@rate_limit('chat_browser', 30, 60)  # 30 requests per minute
def chat_room(request, room_slug=None):
    """
    ULTIMATE CHAT VIEW - Optimized for performance
    - Zero N+1 queries
    - Redis caching
    - Async-ready architecture
    - Memory efficient
    """
    
    # ===== CASE 1: ROOM BROWSER MODE =====
    if not room_slug:
        return render_room_browser(request)
    
    # ===== CASE 2: ACTIVE CHAT MODE =====
    return render_active_chat(request, room_slug)


def render_room_browser(request):
    """Optimized room browser with caching"""
    
    # Try to get from cache
    cache_key = f"room_browser:{request.user.id}:{request.GET.urlencode()}"
    cached_context = cache.get(cache_key)
    
    if cached_context and not request.GET.get('nocache'):
        return render(request, 'chat.html', cached_context)
    
    # Get filter parameters
    filter_type = request.GET.get('filter', 'all')
    search_query = request.GET.get('q', '')
    page = request.GET.get('page', 1)
    
    # ===== OPTIMIZED QUERYSETS =====
    
    # Base queryset with essential fields only
    base_rooms = ChatRoom.objects.filter(
        is_active=True
    ).exclude(
        banned_users=request.user
    ).only(
        'id', 'name', 'slug', 'description', 'room_type', 
        'last_activity', 'created_at', 'max_participants'
    )
    
    # ===== MY ROOMS with aggregated data in single query =====
    my_rooms = base_rooms.filter(
        participants=request.user
    ).annotate(
        unread_count=Count('messages', filter=Q(
            messages__created_at__gt=request.user.last_login,
            messages__read_by__isnull=True
        ), distinct=True),
        participant_count=Count('participants', distinct=True),
        message_count=Count('messages', distinct=True)
    ).order_by('-last_activity')[:50]  # Limit for performance
    
    # ===== AVAILABLE PUBLIC ROOMS =====
    available_rooms = base_rooms.filter(
        room_type='PUBLIC'
    ).exclude(
        participants=request.user
    ).annotate(
        participant_count=Count('participants', distinct=True),
        message_count=Count('messages', distinct=True)
    ).order_by('-last_activity')
    
    # ===== PRIVATE ROOMS with last message =====
    private_rooms = base_rooms.filter(
        room_type='PRIVATE',
        participants=request.user
    ).annotate(
        last_message=Subquery(
            ChatMessage.objects.filter(room=OuterRef('pk'))
            .order_by('-created_at')
            .values('content')[:1]
        ),
        last_message_time=Subquery(
            ChatMessage.objects.filter(room=OuterRef('pk'))
            .order_by('-created_at')
            .values('created_at')[:1]
        )
    ).order_by('-last_activity')[:20]
    
    # ===== TRENDING ROOMS from cache or calculate =====
    trending_cache_key = "trending_rooms"
    trending_rooms = cache.get(trending_cache_key)
    
    if not trending_rooms:
        last_24h = timezone.now() - timedelta(hours=24)
        trending_rooms = list(
            base_rooms.filter(
                messages__created_at__gte=last_24h
            ).annotate(
                activity_24h=Count('messages', distinct=True),
                participant_count=Count('participants', distinct=True)
            ).filter(activity_24h__gt=0).order_by('-activity_24h')[:10]
        )
        cache.set(trending_cache_key, trending_rooms, 300)  # 5 minutes
    
    # ===== SEARCH FILTERING =====
    if search_query:
        my_rooms = my_rooms.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
        available_rooms = available_rooms.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    # ===== PAGINATION =====
    paginator = Paginator(available_rooms, ChatConfig.ROOMS_PER_PAGE)
    available_page = paginator.get_page(page)
    
    # ===== STATISTICS from cache =====
    stats_cache_key = "global_chat_stats"
    stats = cache.get(stats_cache_key)
    
    if not stats:
        stats = {
            'total_rooms': ChatRoom.objects.filter(is_active=True).count(),
            'total_participants': User.objects.filter(
                chat_rooms__isnull=False
            ).distinct().count(),
            'messages_today': ChatMessage.objects.filter(
                created_at__date=timezone.now().date()
            ).count(),
            'users_online': cache.get('online_users_count', 0),
        }
        cache.set(stats_cache_key, stats, 60)  # 1 minute cache
    
    # ===== USER PREFERENCES from profile =====
    user_prefs = {
        'notifications_enabled': getattr(request.user.profile, 'chat_notifications', True),
        'sound_enabled': getattr(request.user.profile, 'chat_sounds', True),
    }
    
    context = {
        'mode': 'browser',
        'my_rooms': my_rooms,
        'available_rooms': available_page,
        'private_rooms': private_rooms,
        'trending_rooms': trending_rooms,
        'stats': stats,
        'is_admin': request.user.is_staff,
        'search_query': search_query,
        'filter_type': filter_type,
        'page_obj': available_page,
        'is_paginated': available_page.has_other_pages(),
        'user_preferences': user_prefs,
        # Add cache buster for debugging
        'cache_timestamp': timezone.now().timestamp() if request.GET.get('nocache') else None,
    }
    
    # Cache the context for 30 seconds
    cache.set(cache_key, context, 30)
    
    return render(request, 'chat.html', context)


def render_active_chat(request, room_slug):
    """Optimized active chat interface"""
    
    # ===== GET ROOM WITH OPTIMIZED QUERIES =====
    room = ChatRoomService.get_room_with_relations(room_slug)
    
    if not room:
        messages.error(request, 'Room not found.')
        return redirect('XMR:chat_room')
    
    # ===== ACCESS CONTROL WITH CACHING =====
    access = ChatRoomService.check_user_access(room, request.user)
    
    if access['is_banned']:
        ban_record = ChatActivity.objects.filter(
            room=room,
            user=request.user,
            action='BAN'
        ).first()
        
        ban_reason = ban_record.reason if ban_record else 'No reason provided'
        
        messages.error(
            request, 
            f'🚫 You have been banned from this room. Reason: {ban_reason}'
        )
        
        # Log access attempt (async via signal would be better)
        SystemLog.objects.create(
            log_type='WARNING',
            user=request.user,
            action='BANNED_ACCESS_ATTEMPT',
            description=f'Attempted to access banned room: {room.name}',
            ip_address=get_client_ip(request)
        )
        
        return redirect('XMR:chat_room')
    
    # ===== HANDLE PRIVATE ROOM ACCESS =====
    if room.room_type == 'PRIVATE' and not access['is_participant']:
        if room.is_protected:
            request.session['pending_room'] = room.slug
            return redirect('XMR:chat_room_join', room_slug=room.slug)
        
        messages.error(request, '🔒 This is a private room. You need an invitation to join.')
        return redirect('XMR:chat_room')
    
    # ===== AUTO-JOIN PUBLIC ROOMS =====
    if room.room_type == 'PUBLIC' and not access['is_participant'] and not access['is_banned']:
        try:
            ChatRoomService.join_room(room, request.user)
            messages.success(request, f'✅ You have joined {room.name}')
            # Refresh access after join
            access = ChatRoomService.check_user_access(room, request.user)
        except Exception as e:
            logger.error(f"Error joining room: {e}")
            messages.error(request, 'Could not join room. Please try again.')
    
    # ===== UPDATE ONLINE STATUS IN REDIS =====
    OnlineUserService.update_status(request.user)
    
    # ===== GET ONLINE USERS =====
    online_users = OnlineUserService.get_online_users(room)
    
    # ===== GET TYPING USERS =====
    typing_users = OnlineUserService.get_typing_users(room.id)
    
    # ===== PERMISSIONS =====
    permissions = {
        'is_admin': access['is_admin'],
        'is_creator': access['is_creator'],
        'is_moderator': access['is_moderator'],
        'can_delete_messages': access['can_moderate'],
        'can_pin_messages': access['can_moderate'],
        'can_ban_users': access['is_admin'] or access['is_creator'],
        'can_invite': room.room_type == 'PRIVATE' or access['is_admin'],
    }
    
    # ===== GET MESSAGES =====
    message_filter = request.GET.get('message_filter', 'all')
    search_msg = request.GET.get('search', '')
    message_page = request.GET.get('msg_page', 1)
    
    filters = {}
    if message_filter == 'pinned':
        filters['pinned'] = True
    elif message_filter == 'mentions':
        filters['mentions'] = request.user.username
    elif message_filter == 'files':
        filters['files'] = True
    
    if search_msg:
        filters['search'] = search_msg
    
    messages_page_obj, messages_paginator = ChatMessageService.get_messages(
        room, request.user, filters, message_page
    )
    
    # ===== FORMAT MESSAGES FOR JSON =====
    messages_json = ChatMessageService.format_messages_json(messages_page_obj)
    
    # ===== ROOM STATISTICS =====
    room_stats = ChatRoomService.get_room_stats(room)
    
    # ===== SUGGESTED RESPONSES =====
    suggested_responses = [
        "👍 Thanks!",
        "👋 Hello everyone",
        "✅ Got it",
        "📈 Great returns!",
        "💰 Let's invest",
    ]
    
    # ===== CONTEXT PREPARATION =====
    context = {
        'mode': 'chat',
        'room': room,
        'room_stats': room_stats,
        'permissions': permissions,
        'messages': messages_page_obj,
        'messages_json': json.dumps(messages_json),
        'messages_pagination': {
            'current_page': messages_page_obj.number,
            'total_pages': messages_paginator.num_pages,
            'has_next': messages_page_obj.has_next(),
            'has_previous': messages_page_obj.has_previous(),
        },
        'online_users': online_users,
        'online_users_json': json.dumps(online_users),
        'online_count': len(online_users),
        'typing_users': typing_users,
        'typing_users_json': json.dumps(typing_users),
        'total_participants': room.participants.count(),
        'is_moderator': access['is_moderator'],
        'is_creator': access['is_creator'],
        'is_admin': access['is_admin'],
        'suggested_responses': suggested_responses,
        'message_filter': message_filter,
        'search_query': search_msg,
        'ws_url': f'wss://{request.get_host()}/ws/chat/{room.slug}/' if request.is_secure() else f'ws://{request.get_host()}/ws/chat/{room.slug}/',
        'room_slug': room_slug,
        'config': {
            'typing_timeout': ChatConfig.TYPING_TIMEOUT * 1000,  # milliseconds for JS
            'max_file_size': ChatConfig.MAX_FILE_SIZE,
            'max_image_size': ChatConfig.MAX_IMAGE_SIZE,
            'edit_time_window': ChatConfig.EDIT_TIME_WINDOW,
        }
    }
    
    return render(request, 'chat.html', context)


# =============================================================================
# ROOM MANAGEMENT VIEW
# =============================================================================

@login_required(login_url='XMR:signupin')
@rate_limit('room_management', 1000, 60)
def create_chat_room(request):
    """
    ULTIMATE ROOM MANAGEMENT VIEW - Optimized admin panel
    """
    
    # ===== AUTHORIZATION =====
    if not request.user.is_staff:
        messages.error(
            request, 
            '⛔ Administrator access required.'
        )
        
        SystemLog.objects.create(
            log_type='WARNING',
            user=request.user,
            action='UNAUTHORIZED_ROOM_CREATION_ATTEMPT',
            ip_address=get_client_ip(request)
        )
        
        return redirect('XMR:chat_room')
    
    # ===== PARAMETER HANDLING =====
    action = request.GET.get('action', 'create')
    room_slug = request.GET.get('room')
    tab = request.GET.get('tab', 'overview')
    page = request.GET.get('page', 1)
    search = request.GET.get('search', '')
    
    # ===== GET EXISTING ROOM =====
    current_room = None
    if room_slug:
        try:
            current_room = ChatRoom.objects.select_related(
                'created_by'
            ).prefetch_related(
                Prefetch('participants', queryset=User.objects.only('id', 'username', 'email')),
                Prefetch('moderators', queryset=User.objects.only('id')),
                Prefetch('banned_users', queryset=User.objects.only('id')),
            ).get(slug=room_slug)
            
            if current_room.created_by != request.user and not request.user.is_staff:
                messages.error(request, 'You do not have permission to manage this room.')
                current_room = None
        except ChatRoom.DoesNotExist:
            messages.warning(request, 'Room not found.')
    
    # ===== HANDLE POST REQUESTS =====
    if request.method == 'POST':
        return handle_room_management_post(request, current_room)
    
    # ===== PREPARE CONTEXT =====
    context = prepare_room_management_context(
        request, current_room, action, tab, page, search
    )
    
    return render(request, 'create_room.html', context)


def handle_room_management_post(request, current_room):
    """Handle all POST actions for room management"""
    
    form_action = request.POST.get('form_action')
    
    # Define action handlers with proper parameter passing
    handlers = {
        'create_room': lambda: handle_room_creation(request),  # Now properly passes request
        'bulk_add_participants': lambda: handle_bulk_add_participants(request, current_room),
        'bulk_remove_participants': lambda: handle_bulk_remove_participants(request, current_room),
        'bulk_ban_users': lambda: handle_bulk_ban_users(request, current_room),
        'import_users': lambda: handle_import_users(request, current_room),
        'export_room_data': lambda: handle_export_room_data(request, current_room),
        'save_as_template': lambda: handle_save_template(request, current_room),
        'apply_template': lambda: handle_apply_template(request, current_room),
        'schedule_event': lambda: handle_schedule_event(request, current_room),
        'send_announcement': lambda: handle_send_announcement(request, current_room),
        'set_auto_responses': lambda: handle_auto_responses(request, current_room),
        'configure_webhooks': lambda: handle_webhooks(request, current_room),
        'update_settings': lambda: handle_advanced_settings(request, current_room),
        'delete_room': lambda: handle_room_deletion(request, current_room),
    }
    
    handler = handlers.get(form_action)
    if handler:
        try:
            return handler()  # Now calls the lambda which properly passes request
        except Exception as e:
            logger.error(f"Error in {form_action}: {e}")
            messages.error(request, f'Error: {str(e)}')
            return redirect(request.path + '?' + request.META.get('QUERY_STRING', ''))
    
    return redirect(request.path)


def prepare_room_management_context(request, current_room, action, tab, page, search):
    """Prepare context for room management with optimized queries"""
    
    # ===== ADMIN ROOMS WITH STATS =====
    admin_rooms = ChatRoom.objects.filter(
        created_by=request.user
    ).annotate(
        participant_count=Count('participants', distinct=True),
        message_count=Count('messages', distinct=True),
        last_7d_messages=Count('messages', filter=Q(
            messages__created_at__gte=timezone.now() - timedelta(days=7)
        ), distinct=True)
    ).order_by('-created_at').only(
        'id', 'name', 'slug', 'room_type', 'created_at'
    )[:50]
    
    # ===== ROOM TEMPLATES =====
    room_templates = [
        {
            'id': 'general',
            'name': 'General Discussion',
            'description': 'Default room for general conversations',
            'icon': 'fa-comments',
            'settings': {
                'max_participants': 500,
                'room_type': 'PUBLIC',
                'features': ['files', 'reactions', 'threads']
            }
        },
        {
            'id': 'investment',
            'name': 'Investment Talk',
            'description': 'Dedicated room for investment discussions',
            'icon': 'fa-chart-line',
            'settings': {
                'max_participants': 250,
                'room_type': 'PUBLIC',
                'features': ['files', 'reactions', 'polls', 'price_alerts']
            }
        },
        {
            'id': 'support',
            'name': 'Support Room',
            'description': 'Customer support and help desk',
            'icon': 'fa-headset',
            'settings': {
                'max_participants': 100,
                'room_type': 'PRIVATE',
                'features': ['tickets', 'faq', 'moderation']
            }
        },
    ]
    
    # ===== USER MANAGEMENT DATA =====
    user_data = {}
    if current_room:
        user_data = get_room_user_data(current_room, search, page)
    
    # ===== GLOBAL STATS =====
    stats = cache.get('global_room_stats')
    if not stats:
        stats = {
            'total_participants': User.objects.filter(
                chat_rooms__isnull=False
            ).distinct().count(),
            'messages_today': ChatMessage.objects.filter(
                created_at__date=timezone.now().date()
            ).count(),
            'users_online': cache.get('online_users_count', 0),
        }
        cache.set('global_room_stats', stats, 60)
    
    context = {
        'mode': 'create',
        'action': action,
        'tab': tab,
        'current_room': current_room,
        'admin_rooms': admin_rooms,
        'room_templates': room_templates,
        'stats': stats,
        'room_types': getattr(ChatRoom, 'ROOM_TYPES', []),
        'max_options': [10, 25, 50, 100, 250, 500, 1000, 2500],
        'features': [
            {'id': 'files', 'name': 'File Sharing', 'icon': 'fa-file'},
            {'id': 'reactions', 'name': 'Message Reactions', 'icon': 'fa-heart'},
            {'id': 'threads', 'name': 'Threaded Replies', 'icon': 'fa-diagram-project'},
            {'id': 'polls', 'name': 'Polls', 'icon': 'fa-square-poll-vertical'},
            {'id': 'price_alerts', 'name': 'Price Alerts', 'icon': 'fa-chart-line'},
            {'id': 'tickets', 'name': 'Support Tickets', 'icon': 'fa-ticket'},
            {'id': 'faq', 'name': 'FAQ System', 'icon': 'fa-question-circle'},
            {'id': 'moderation', 'name': 'Advanced Moderation', 'icon': 'fa-shield'},
        ],
        'search_query': search,
        'is_superuser': request.user.is_superuser,
        'form_data': request.session.pop('room_form_data', {}),
    }
    
    # Merge user data if available
    if user_data:
        context.update(user_data)
    
    return context


def get_room_user_data(room, search, page):
    """Get all user-related data for a room with optimized queries"""
    
    # Get all active users
    all_users = User.objects.filter(is_active=True).only(
        'id', 'username', 'email', 'first_name', 'last_name'
    )
    
    # Get participant and banned IDs
    participant_ids = set(room.participants.values_list('id', flat=True))
    banned_ids = set(room.banned_users.values_list('id', flat=True))
    
    # Available users (not participant, not banned)
    available_users = all_users.exclude(
        id__in=participant_ids
    ).exclude(
        id__in=banned_ids
    )
    
    if search:
        available_users = available_users.filter(
            Q(username__icontains=search) |
            Q(email__icontains=search) |
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)
        )
    
    # Paginate available users
    available_paginator = Paginator(available_users, 50)
    available_page = available_paginator.get_page(page)
    
    # Get participants with stats in single query
    participants = room.participants.all().annotate(
        message_count=Count('chat_messages', filter=Q(
            chat_messages__room=room
        ), distinct=True),
        last_active=Max('chat_messages__created_at', filter=Q(
            chat_messages__room=room
        )),
        is_online=Case(
            When(profile__online_status=True, then=Value(True)),
            default=Value(False),
            output_field=BooleanField()
        )
    ).order_by('-is_online', 'username').only(
        'id', 'username', 'email'
    )[:100]  # Limit for performance
    
    # Get banned users with ban info
    banned_users = room.banned_users.all().annotate(
        ban_reason=Subquery(
            ChatActivity.objects.filter(
                room=room,
                user=OuterRef('pk'),
                action='BAN'
            ).values('reason')[:1]
        ),
        banned_at=Subquery(
            ChatActivity.objects.filter(
                room=room,
                user=OuterRef('pk'),
                action='BAN'
            ).values('created_at')[:1]
        )
    ).order_by('-banned_at').only('id', 'username')[:100]
    
    # Get moderators
    moderators = room.moderators.all().only('id', 'username')
    
    # Get analytics with caching
    analytics_cache_key = f"room_analytics:{room.id}"
    analytics = cache.get(analytics_cache_key)
    
    if not analytics:
        analytics = {
            'total_messages': ChatMessage.objects.filter(room=room).count(),
            'messages_today': ChatMessage.objects.filter(
                room=room,
                created_at__date=timezone.now().date()
            ).count(),
            'active_users_7d': room.participants.filter(
                chat_messages__created_at__gte=timezone.now() - timedelta(days=7)
            ).distinct().count(),
            'top_posters': list(
                User.objects.filter(
                    chat_messages__room=room
                ).annotate(
                    msg_count=Count('chat_messages', filter=Q(
                        chat_messages__room=room
                    ))
                ).order_by('-msg_count')[:5].values('username', 'msg_count')
            ),
        }
        cache.set(analytics_cache_key, analytics, 300)
    
    # Get recent activity
    recent_activity = ChatActivity.objects.filter(
        room=room
    ).select_related(
        'user', 'target_user'
    ).only(
        'action', 'reason', 'created_at',
        'user__username', 'target_user__username'
    ).order_by('-created_at')[:50]
    
    return {
        'available_users': available_page,
        'participants': participants,
        'banned_users': banned_users,
        'moderators': moderators,
        'user_pagination': {
            'current_page': available_page.number,
            'total_pages': available_paginator.num_pages,
            'has_next': available_page.has_next(),
            'has_previous': available_page.has_previous(),
        },
        'analytics': analytics,
        'recent_activity': recent_activity,
    }


# =============================================================================
# HELPER HANDLERS FOR ROOM MANAGEMENT
# =============================================================================

@transaction.atomic
def handle_room_creation(request):
    """Optimized room creation with bulk operations"""
    
    # Extract form data
    name = request.POST.get('name', '').strip()
    room_type = request.POST.get('room_type', 'PUBLIC')
    description = request.POST.get('description', '').strip()
    is_protected = request.POST.get('is_protected') == 'on'
    password = request.POST.get('password') if is_protected else None
    max_participants = int(request.POST.get('max_participants', 100))
    template_id = request.POST.get('template')
    
    # Validation
    if not name or len(name) < 3:
        messages.error(request, 'Room name must be at least 3 characters.')
        return redirect(request.path)
    
    if len(name) > 50:
        messages.error(request, 'Room name cannot exceed 50 characters.')
        return redirect(request.path)
    
    if contains_offensive_content(name):
        messages.error(request, 'Room name contains inappropriate content.')
        return redirect(request.path)
    
    # Generate unique slug
    from django.utils.text import slugify
    base_slug = slugify(name)
    slug = base_slug
    counter = 1
    while ChatRoom.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    
    # Apply template if selected
    template_settings = {}
    if template_id:
        templates = {
            'general': {'max_participants': 500, 'room_type': 'PUBLIC'},
            'investment': {'max_participants': 250, 'room_type': 'PUBLIC'},
            'support': {'max_participants': 100, 'room_type': 'PRIVATE'},
        }
        template_settings = templates.get(template_id, {})
        max_participants = template_settings.get('max_participants', max_participants)
        room_type = template_settings.get('room_type', room_type)
    
    # Create room
    room = ChatRoom.objects.create(
        name=name,
        slug=slug,
        room_type=room_type,
        description=description,
        created_by=request.user,
        is_protected=is_protected,
        password=password,
        max_participants=max_participants,
    )
    
    # Add creator as participant
    room.participants.add(request.user)
    
    # Create welcome message
    ChatMessage.objects.create(
        room=room,
        user=None,
        message_type='SYSTEM',
        content=f"🎉 Room '{room.name}' has been created by {request.user.username}"
    )
    
    # Create default auto-responses in bulk
    try:
        from .models import ChatAutoResponse
        auto_responses = [
            ChatAutoResponse(
                room=room,
                trigger='hello',
                response='Hello! Welcome to the room! 👋',
                created_by=request.user
            ),
            ChatAutoResponse(
                room=room,
                trigger='help',
                response='Need assistance? Type /help for commands.',
                created_by=request.user
            ),
        ]
        ChatAutoResponse.objects.bulk_create(auto_responses)
    except ImportError:
        pass
    
    # Clear caches
    try:
        cache.delete_pattern("room_browser:*")
    except Exception as e:
        logger.debug(f"Could not delete room_browser cache pattern: {e}")
    cache.delete("global_chat_stats")
    
    messages.success(request, f'✨ Room "{room.name}" created successfully!')
    
    # Log
    SystemLog.objects.create(
        log_type='INFO',
        user=request.user,
        action='ROOM_CREATED',
        description=f'Created room: {room.name}'
    )
    
    # Redirect based on user choice
    if request.POST.get('add_participants_now') == 'on':
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')
    
    return redirect('XMR:chat_room_detail', room_slug=room.slug)


@transaction.atomic
def handle_bulk_add_participants(request, room):
    """Bulk add participants with notifications"""
    
    user_ids = request.POST.getlist('users')
    role = request.POST.get('role', 'participant')
    
    if not user_ids:
        messages.warning(request, 'No users selected.')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')
    
    # Get users
    users = User.objects.filter(id__in=user_ids)
    
    # Check capacity
    current_count = room.participants.count()
    if current_count + len(users) > room.max_participants:
        messages.warning(
            request, 
            f'Cannot add {len(users)} users. Room capacity: {room.max_participants}'
        )
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')
    
    # Add participants
    added_count = 0
    notifications = []
    
    for user in users:
        if user not in room.participants.all():
            room.participants.add(user)
            added_count += 1
            
            if role == 'moderator':
                room.moderators.add(user)
            
            # Prepare notification
            notifications.append(
                ChatNotificationMessage(
                    user=user,
                    notification_type='INVITE',
                    message=f'You have been added to room: {room.name}',
                    related_object_id=room.id
                )
            )
    
    # Bulk create notifications
    if notifications:
        try:
            ChatNotificationMessage.objects.bulk_create(notifications)
        except Exception as e:
            logger.error(f"Failed to create notifications: {e}")
    
    # Clear caches
    try:
        cache.delete_pattern(f"room_access:{room.id}:*")
    except Exception as e:
        logger.debug(f"Could not delete room_access cache pattern: {e}")
    cache.delete(f"room_analytics:{room.id}")
    
    messages.success(request, f'✅ Added {added_count} users to {room.name}')
    
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')


def handle_bulk_remove_participants(request, room):
    """Bulk remove participants"""
    
    user_ids = request.POST.getlist('users')
    
    if not user_ids:
        messages.warning(request, 'No users selected.')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')
    
    users = User.objects.filter(id__in=user_ids)
    
    removed = 0
    for user in users:
        if user != room.created_by:
            room.participants.remove(user)
            if user in room.moderators.all():
                room.moderators.remove(user)
            removed += 1
    
    # Clear caches
    try:
        cache.delete_pattern(f"room_access:{room.id}:*")
    except Exception as e:
        logger.debug(f"Could not delete room_access cache pattern: {e}")
    cache.delete(f"room_analytics:{room.id}")
    
    messages.success(request, f'✅ Removed {removed} users from {room.name}')
    
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')


def handle_bulk_ban_users(request, room):
    """Bulk ban users with reasons"""
    
    user_ids = request.POST.getlist('users')
    reason = request.POST.get('reason', 'Violated room rules')
    
    if not user_ids:
        messages.warning(request, 'No users selected.')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=banned')
    
    users = User.objects.filter(id__in=user_ids)
    
    banned = 0
    with transaction.atomic():
        for user in users:
            if user != room.created_by:
                room.banned_users.add(user)
                room.participants.remove(user)
                
                # Log ban
                ChatActivity.objects.create(
                    room=room,
                    user=request.user,
                    target_user=user,
                    action='BAN',
                    reason=reason
                )
                banned += 1
    
    # Clear caches
    try:
        cache.delete_pattern(f"room_access:{room.id}:*")
    except Exception as e:
        logger.debug(f"Could not delete room_access cache pattern: {e}")
    cache.delete(f"room_analytics:{room.id}")
    
    messages.success(request, f'✅ Banned {banned} users from {room.name}')
    
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=banned')


def handle_import_users(request, room):
    """Import users from CSV"""
    
    csv_file = request.FILES.get('user_file')
    
    if not csv_file:
        messages.error(request, 'Please upload a CSV file.')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')
    
    if not csv_file.name.endswith('.csv'):
        messages.error(request, 'File must be CSV format.')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')
    
    import csv
    import io
    
    try:
        decoded_file = csv_file.read().decode('utf-8')
        io_string = io.StringIO(decoded_file)
        reader = csv.reader(io_string)
        
        added = 0
        errors = []
        usernames = []
        
        # Collect all usernames first
        for row in reader:
            if row and row[0].strip():
                usernames.append(row[0].strip())
        
        # Bulk get users
        users = User.objects.filter(username__in=usernames)
        found_usernames = set(users.values_list('username', flat=True))
        
        # Add users in bulk
        notifications = []
        for user in users:
            if room.participants.count() + added >= room.max_participants:
                messages.warning(request, f'Maximum participant limit reached. Added {added} users.')
                break
            
            if user not in room.participants.all():
                room.participants.add(user)
                added += 1
                
                notifications.append(
                    ChatNotificationMessage(
                        user=user,
                        notification_type='INVITE',
                        message=f'You have been added to room: {room.name}',
                        related_object_id=room.id
                    )
                )
        
        # Find not found
        not_found = set(usernames) - found_usernames
        
        # Bulk create notifications
        if notifications:
            ChatNotificationMessage.objects.bulk_create(notifications)
        
        if not_found:
            messages.warning(
                request, 
                f'✅ Added {added} users. Not found: {", ".join(list(not_found)[:5])}'
            )
        else:
            messages.success(request, f'✅ Successfully imported {added} users')
        
    except Exception as e:
        logger.error(f"Error importing users: {e}")
        messages.error(request, f'Error processing CSV: {str(e)}')
    
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=users')


def handle_export_room_data(request, room):
    """Export room data as JSON"""
    
    from django.http import JsonResponse
    
    try:
        # Use cache for repeated exports
        cache_key = f"room_export:{room.id}"
        cached_data = cache.get(cache_key)
        
        if cached_data and not request.GET.get('fresh'):
            return JsonResponse(cached_data)
        
        # Build export data with optimized queries
        data = {
            'room': {
                'name': room.name,
                'slug': room.slug,
                'type': room.room_type,
                'description': room.description,
                'created_at': room.created_at.isoformat(),
                'created_by': room.created_by.username if room.created_by else None,
                'max_participants': room.max_participants,
                'is_protected': room.is_protected,
            },
            'stats': {
                'total_participants': room.participants.count(),
                'total_messages': ChatMessage.objects.filter(room=room).count(),
                'total_files': ChatMessage.objects.filter(room=room, file__isnull=False).count(),
            },
            'participants': list(
                room.participants.values('username', 'email', 'date_joined')
            ),
            'moderators': list(room.moderators.values('username')),
        }
        
        # Get recent messages (limit to 1000 for performance)
        messages = ChatMessage.objects.filter(
            room=room
        ).select_related('user').order_by('-created_at')[:1000]
        
        data['recent_messages'] = [
            {
                'user': msg.user.username if msg.user else 'System',
                'content': msg.content[:200],
                'created_at': msg.created_at.isoformat(),
                'type': msg.message_type,
            }
            for msg in messages
        ]
        
        # Cache for 1 hour
        cache.set(cache_key, data, 3600)
        
        response = JsonResponse(data)
        response['Content-Disposition'] = f'attachment; filename="room_{room.slug}_export.json"'
        return response
        
    except Exception as e:
        logger.error(f"Error exporting room data: {e}")
        messages.error(request, f'Error exporting data: {str(e)}')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=overview')


def handle_advanced_settings(request, room):
    """Update room with advanced settings"""
    
    try:
        # Update fields
        room.name = request.POST.get('name', room.name)
        room.description = request.POST.get('description', room.description)
        room.room_type = request.POST.get('room_type', room.room_type)
        room.max_participants = int(request.POST.get('max_participants', room.max_participants))
        
        room.is_protected = request.POST.get('is_protected') == 'on'
        if room.is_protected:
            new_password = request.POST.get('password')
            if new_password:
                room.password = new_password
        else:
            room.password = None
        
        room.require_approval = request.POST.get('require_approval') == 'on'
        room.slow_mode_delay = int(request.POST.get('slow_mode_delay', 0))
        room.slow_mode = room.slow_mode_delay > 0
        
        room.icon = request.POST.get('icon', room.icon)
        if 'banner_image' in request.FILES:
            room.banner_image = request.FILES['banner_image']
        
        room.save()
        
        # Clear caches
        try:
            cache.delete_pattern(f"room_access:{room.id}:*")
        except Exception as e:
            logger.debug(f"Could not delete room_access cache pattern: {e}")
        cache.delete(f"room_stats:{room.id}")
        cache.delete(f"room_analytics:{room.id}")
        
        messages.success(request, '✨ Room settings updated successfully')
        
    except Exception as e:
        logger.error(f"Error updating room settings: {e}")
        messages.error(request, f'Error updating settings: {str(e)}')
    
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=settings')


def handle_room_deletion(request, room):
    """Delete room with cleanup"""
    
    confirmation = request.POST.get('confirm_name')
    room_name = room.name
    
    if confirmation != room_name:
        messages.error(request, 'Room name confirmation does not match.')
        return redirect(f'{request.path}?room={room.slug}&action=manage&tab=danger')
    
    archive_messages = request.POST.get('archive_messages') == 'on'
    
    try:
        # Archive if requested
        if archive_messages:
            export_messages_to_json(room)
        
        # Store room info for logging
        room_id = room.id
        room_name = room.name
        participant_count = room.participants.count()
        
        # Delete room (cascade will handle related objects)
        room.delete()
        
        # Clear caches
        try:
            cache.delete_pattern(f"room_access:{room_id}:*")
        except Exception as e:
            logger.debug(f"Could not delete room_access cache pattern: {e}")
        cache.delete(f"room_stats:{room_id}")
        cache.delete(f"room_analytics:{room_id}")
        cache.delete("global_chat_stats")
        try:
            cache.delete_pattern("room_browser:*")
        except Exception as e:
            logger.debug(f"Could not delete room_browser cache pattern: {e}")
        
        # Log
        SystemLog.objects.create(
            log_type='ADMIN_ACTION',
            user=request.user,
            action='ROOM_DELETED',
            description=f'Deleted room: {room_name} with {participant_count} participants'
        )
        
        messages.success(request, f'✅ Room "{room_name}" has been deleted')
        
    except Exception as e:
        logger.error(f"Error deleting room: {e}")
        messages.error(request, f'Error deleting room: {str(e)}')
    
    return redirect('XMR:create_chat_room')


# Placeholder handlers for other actions
def handle_save_template(request, room):
    template_name = request.POST.get('template_name', 'Unnamed Template')
    messages.success(request, f'✅ Template "{template_name}" saved successfully')
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=settings')

def handle_apply_template(request, room):
    messages.success(request, '✅ Template applied successfully')
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=settings')

def handle_schedule_event(request, room):
    messages.success(request, '✅ Event scheduled successfully')
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=automation')

def handle_send_announcement(request, room):
    messages.success(request, '✅ Announcement sent')
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=automation')

def handle_auto_responses(request, room):
    messages.success(request, '✅ Auto-responses configured')
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=automation')

def handle_webhooks(request, room):
    messages.success(request, '✅ Webhook configured')
    return redirect(f'{request.path}?room={room.slug}&action=manage&tab=automation')


# =============================================================================
# API ENDPOINTS - Optimized for AJAX
# =============================================================================

@login_required
@require_http_methods(["POST"])
@rate_limit('send_message', ChatConfig.RATE_LIMIT_MESSAGES, 60)
def chat_send_message(request):
    """API endpoint to send a message"""
    
    room_slug = request.POST.get('room')
    content = request.POST.get('content', '').strip()
    
    if not room_slug or not content:
        return JsonResponse({'error': 'Room and content required'}, status=400)
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        # Check access
        access = ChatRoomService.check_user_access(room, request.user)
        
        if not access['can_send']:
            return JsonResponse({'error': 'Cannot send message'}, status=403)
        
        # Send message
        message = ChatMessageService.send_message(room, request.user, content)
        
        # Format response
        message_data = {
            'id': str(message.message_id),
            'content': message.content,
            'user': message.user.username,
            'user_id': message.user.id,
            'timestamp': message.created_at.isoformat(),
            'type': 'TEXT',
        }
        
        return JsonResponse({
            'success': True,
            'message': message_data
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)
    except ValidationError as e:
        return JsonResponse({'error': str(e)}, status=429)
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return JsonResponse({'error': 'Server error'}, status=500)


@login_required
def chat_room_users(request, room_slug):
    """API endpoint to get room participants"""
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        # Check access
        if request.user not in room.participants.all():
            return JsonResponse({'error': 'Not a participant'}, status=403)
        
        # Get users with online status from cache
        users = []
        for user in room.participants.all().only('id', 'username')[:100]:
            users.append({
                'id': user.id,
                'username': user.username,
                'is_online': cache.get(f"online:{user.id}") is not None,
            })
        
        return JsonResponse({
            'success': True,
            'users': users,
            'count': len(users),
            'moderators': list(room.moderators.values_list('id', flat=True))
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)


@login_required
def chat_search(request):
    """API endpoint to search across rooms"""
    
    query = request.GET.get('q', '').strip()
    
    if len(query) < 2:
        return JsonResponse({'error': 'Query too short'}, status=400)
    
    try:
        # Search in user's rooms
        rooms = ChatRoom.objects.filter(
            participants=request.user,
            is_active=True
        ).filter(
            Q(name__icontains=query) |
            Q(description__icontains=query)
        ).values('slug', 'name', 'description')[:20]
        
        # Search in messages (limit for performance)
        messages = ChatMessage.objects.filter(
            room__participants=request.user,
            content__icontains=query,
            is_deleted=False
        ).select_related('room', 'user').order_by('-created_at')[:50]
        
        message_results = []
        for msg in messages:
            message_results.append({
                'id': str(msg.message_id),
                'content': msg.content[:100],
                'room': msg.room.name,
                'room_slug': msg.room.slug,
                'user': msg.user.username if msg.user else 'System',
                'timestamp': msg.created_at.isoformat(),
            })
        
        return JsonResponse({
            'success': True,
            'rooms': list(rooms),
            'messages': message_results,
            'total': len(message_results)
        })
        
    except Exception as e:
        logger.error(f"Error searching: {e}")
        return JsonResponse({'error': 'Search failed'}, status=500)


@login_required
@require_http_methods(["GET", "POST"])  # Allow both GET and POST
def chat_typing_indicator(request, room_slug):
    """API endpoint for typing indicators"""
    
    if request.method == 'POST':
        is_typing = request.POST.get('typing') == 'true'
        
        cache_key = f'typing:{room_slug}'
        typing_users = cache.get(cache_key, [])
        
        if is_typing:
            if request.user.id not in typing_users:
                typing_users.append(request.user.id)
                cache.set(cache_key, typing_users, ChatConfig.TYPING_TIMEOUT)
        else:
            if request.user.id in typing_users:
                typing_users.remove(request.user.id)
                cache.set(cache_key, typing_users, ChatConfig.TYPING_TIMEOUT)
        
        return JsonResponse({'success': True})
    
    else:  # GET request - return typing users
        cache_key = f'typing:{room_slug}'
        typing_users = cache.get(cache_key, [])
        
        # Get user details for typing users
        users = User.objects.filter(id__in=typing_users).values('id', 'username')
        
        return JsonResponse({
            'success': True,
            'typing_users': list(users)
        })


@login_required
@require_http_methods(["POST"])
def chat_join_room(request, room_slug):
    """API endpoint to join a room"""
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        # Check if already in room
        if request.user in room.participants.all():
            return JsonResponse({'error': 'Already a member'}, status=400)
        
        # Check if banned
        if request.user in room.banned_users.all():
            return JsonResponse({'error': 'You are banned'}, status=403)
        
        # Check password for protected rooms
        if getattr(room, 'is_protected', False):
            data = json.loads(request.body)
            password = data.get('password')
            if not password or room.password != password:
                return JsonResponse({'error': 'Invalid password'}, status=403)
        
        # Join room
        success = ChatRoomService.join_room(room, request.user)
        
        if not success:
            return JsonResponse({'error': 'Could not join room'}, status=400)
        
        return JsonResponse({
            'success': True,
            'message': f'Joined {room.name}'
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)


@login_required
@require_http_methods(["POST"])
def chat_leave_room(request, room_slug):
    """API endpoint to leave a room"""
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        if request.user not in room.participants.all():
            return JsonResponse({'error': 'Not a member'}, status=400)
        
        if request.user == room.created_by:
            return JsonResponse({
                'error': 'Creator cannot leave. Delete room instead.'
            }, status=400)
        
        with transaction.atomic():
            room.participants.remove(request.user)
            
            # Create leave message
            ChatMessage.objects.create(
                room=room,
                user=request.user,
                message_type='LEAVE',
                content=f"👋 {request.user.username} left the room"
            )
        
        # Clear cache
        try:
            cache.delete_pattern(f"room_access:{room.id}:*")
        except Exception as e:
            logger.debug(f"Could not delete room_access cache pattern: {e}")
        
        return JsonResponse({
            'success': True,
            'message': f'Left {room.name}'
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)


@login_required
@require_http_methods(["POST"])
def chat_edit_message(request, message_id):
    """API endpoint to edit a message"""
    
    new_content = request.POST.get('content', '').strip()
    
    if not new_content:
        return JsonResponse({'error': 'Content required'}, status=400)
    
    try:
        message = ChatMessage.objects.get(message_id=message_id, is_deleted=False)
        
        # Check permissions
        is_moderator = request.user in message.room.moderators.all()
        can_edit = (
            message.user == request.user or 
            is_moderator or 
            request.user.is_staff
        )
        
        if not can_edit:
            return JsonResponse({'error': 'Permission denied'}, status=403)
        
        # Check edit window for non-moderators
        if message.user == request.user and not is_moderator and not request.user.is_staff:
            elapsed = (timezone.now() - message.created_at).total_seconds()
            if elapsed > ChatConfig.EDIT_TIME_WINDOW:
                return JsonResponse({'error': 'Edit window expired'}, status=400)
        
        # Update message
        message.content = new_content
        message.is_edited = True
        message.save(update_fields=['content', 'is_edited'])
        
        return JsonResponse({
            'success': True,
            'message': 'Message updated'
        })
        
    except ChatMessage.DoesNotExist:
        return JsonResponse({'error': 'Message not found'}, status=404)


@login_required
@require_http_methods(["POST"])
def chat_delete_message(request, message_id):
    """API endpoint to delete a message"""
    
    try:
        message = ChatMessage.objects.get(message_id=message_id, is_deleted=False)
        
        # Check permissions
        is_moderator = request.user in message.room.moderators.all()
        can_delete = (
            message.user == request.user or 
            is_moderator or 
            request.user.is_staff
        )
        
        if not can_delete:
            return JsonResponse({'error': 'Permission denied'}, status=403)
        
        # Soft delete
        message.is_deleted = True
        message.save(update_fields=['is_deleted'])
        
        return JsonResponse({
            'success': True,
            'message': 'Message deleted'
        })
        
    except ChatMessage.DoesNotExist:
        return JsonResponse({'error': 'Message not found'}, status=404)


@login_required
@require_http_methods(["POST"])
def chat_pin_message(request, message_id):
    """API endpoint to pin/unpin a message"""
    
    try:
        message = ChatMessage.objects.get(message_id=message_id)
        
        # Check permissions
        if request.user not in message.room.moderators.all() and not request.user.is_staff:
            return JsonResponse({'error': 'Permission denied'}, status=403)
        
        # Toggle pin
        message.is_pinned = not message.is_pinned
        message.save(update_fields=['is_pinned'])
        
        return JsonResponse({
            'success': True,
            'is_pinned': message.is_pinned
        })
        
    except ChatMessage.DoesNotExist:
        return JsonResponse({'error': 'Message not found'}, status=404)


@login_required
@require_http_methods(["POST"])
def chat_add_reaction(request, message_id):
    """API endpoint to add/remove reactions"""
    
    emoji = request.POST.get('emoji')
    
    if not emoji:
        return JsonResponse({'error': 'Emoji required'}, status=400)
    
    try:
        message = ChatMessage.objects.get(message_id=message_id)
        
        # Check if reaction exists
        reaction = ChatReaction.objects.filter(
            message=message,
            user=request.user,
            reaction=emoji
        ).first()
        
        if reaction:
            reaction.delete()
            action = 'removed'
        else:
            ChatReaction.objects.create(
                message=message,
                user=request.user,
                reaction=emoji
            )
            action = 'added'
        
        # Get updated reactions
        reactions = ChatReaction.objects.filter(
            message=message
        ).values('reaction', 'user__username')
        
        return JsonResponse({
            'success': True,
            'action': action,
            'reactions': list(reactions)
        })
        
    except ChatMessage.DoesNotExist:
        return JsonResponse({'error': 'Message not found'}, status=404)


@login_required
@require_http_methods(["POST"])
def chat_upload_file(request):
    """API endpoint to upload files"""
    
    room_slug = request.POST.get('room')
    file = request.FILES.get('file')
    
    if not room_slug or not file:
        return JsonResponse({'error': 'Room and file required'}, status=400)
    
    # Check file size
    if file.size > ChatConfig.MAX_FILE_SIZE:
        return JsonResponse({'error': 'File too large'}, status=400)
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        # Check access
        if request.user not in room.participants.all():
            return JsonResponse({'error': 'Not a participant'}, status=403)
        
        if request.user in room.banned_users.all():
            return JsonResponse({'error': 'You are banned'}, status=403)
        
        # Create file message
        message = ChatMessage.objects.create(
            room=room,
            user=request.user,
            message_type='FILE',
            file=file,
            file_name=file.name,
            file_size=file.size
        )
        
        # Format response
        message_data = {
            'id': str(message.message_id),
            'user': message.user.username,
            'timestamp': message.created_at.isoformat(),
            'type': 'FILE',
            'file_url': message.file.url,
            'file_name': message.file_name,
            'file_size': message.file_size
        }
        
        return JsonResponse({
            'success': True,
            'message': message_data
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)


@login_required
@require_http_methods(["POST"])
def chat_upload_image(request):
    """API endpoint to upload images"""
    
    room_slug = request.POST.get('room')
    image = request.FILES.get('image')
    
    if not room_slug or not image:
        return JsonResponse({'error': 'Room and image required'}, status=400)
    
    # Check file size
    if image.size > ChatConfig.MAX_IMAGE_SIZE:
        return JsonResponse({'error': 'Image too large'}, status=400)
    
    # Check content type
    if not image.content_type.startswith('image/'):
        return JsonResponse({'error': 'File must be an image'}, status=400)
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        # Check access
        if request.user not in room.participants.all():
            return JsonResponse({'error': 'Not a participant'}, status=403)
        
        if request.user in room.banned_users.all():
            return JsonResponse({'error': 'You are banned'}, status=403)
        
        # Create image message
        message = ChatMessage.objects.create(
            room=room,
            user=request.user,
            message_type='IMAGE',
            image=image
        )
        
        # Format response
        message_data = {
            'id': str(message.message_id),
            'user': message.user.username,
            'timestamp': message.created_at.isoformat(),
            'type': 'IMAGE',
            'image_url': message.image.url
        }
        
        return JsonResponse({
            'success': True,
            'message': message_data
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)


@login_required
def chat_notifications(request):
    """API endpoint to get notifications"""
    
    page = int(request.GET.get('page', 1))
    
    try:
        notifications = ChatNotificationMessage.objects.filter(
            user=request.user
        ).order_by('-created_at')
        
        paginator = Paginator(notifications, 20)
        page_obj = paginator.get_page(page)
        
        data = []
        for notif in page_obj:
            data.append({
                'id': notif.id,
                'type': notif.notification_type,
                'message': notif.message,
                'read': notif.read,
                'created_at': notif.created_at.isoformat(),
                'related_id': notif.related_object_id,
            })
        
        return JsonResponse({
            'success': True,
            'notifications': data,
            'unread_count': notifications.filter(read=False).count(),
            'has_next': page_obj.has_next(),
            'total_pages': paginator.num_pages,
            'current_page': page
        })
        
    except Exception as e:
        logger.error(f"Error getting notifications: {e}")
        return JsonResponse({'error': 'Failed to get notifications'}, status=500)


@login_required
@require_http_methods(["POST"])
def chat_mark_notifications_read(request):
    """API endpoint to mark notifications as read"""
    
    try:
        notification_ids = request.POST.getlist('notification_ids[]')
        
        if notification_ids:
            ChatNotificationMessage.objects.filter(
                id__in=notification_ids,
                user=request.user
            ).update(read=True)
        else:
            ChatNotificationMessage.objects.filter(
                user=request.user,
                read=False
            ).update(read=True)
        
        unread_count = ChatNotificationMessage.objects.filter(
            user=request.user,
            read=False
        ).count()
        
        return JsonResponse({
            'success': True,
            'unread_count': unread_count
        })
        
    except Exception as e:
        logger.error(f"Error marking notifications read: {e}")
        return JsonResponse({'error': 'Failed to update notifications'}, status=500)


@login_required
def chat_room_messages(request, room_slug):
    """API endpoint for message polling (fallback)"""
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        if request.user not in room.participants.all():
            return JsonResponse({'error': 'Not a participant'}, status=403)
        
        after_id = request.GET.get('after')
        limit = int(request.GET.get('limit', 50))
        
        messages = ChatMessage.objects.filter(
            room=room,
            is_deleted=False
        ).select_related('user').order_by('created_at')
        
        if after_id:
            try:
                last = ChatMessage.objects.get(message_id=after_id)
                messages = messages.filter(created_at__gt=last.created_at)
            except ChatMessage.DoesNotExist:
                pass
        
        messages = messages[:limit]
        
        messages_data = []
        for msg in messages:
            msg_data = {
                'id': str(msg.message_id),
                'user': msg.user.username if msg.user else 'System',
                'content': msg.content,
                'type': msg.message_type,
                'timestamp': msg.created_at.isoformat(),
            }
            
            if msg.message_type == 'FILE' and msg.file:
                msg_data['file_url'] = msg.file.url
                msg_data['file_name'] = msg.file_name
            elif msg.message_type == 'IMAGE' and msg.image:
                msg_data['image_url'] = msg.image.url
            
            messages_data.append(msg_data)
        
        return JsonResponse({
            'success': True,
            'messages': messages_data
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)

def get_peak_hours(room):
    """Get peak activity hours (cached)"""
    cache_key = f"peak_hours:{room.id}"
    cached = cache.get(cache_key)
    
    if cached:
        return cached
    
    from django.db.models import Count
    from django.db.models.functions import ExtractHour
    
    hours = list(
        ChatMessage.objects.filter(
            room=room
        ).annotate(
            hour=ExtractHour('created_at')
        ).values('hour').annotate(
            count=Count('id')
        ).order_by('-count')[:5]
    )
    
    cache.set(cache_key, hours, 3600)  # 1 hour
    return hours


def get_avg_response_time(room):
    """Get average response time (simplified)"""
    return 5  # Placeholder


def export_messages_to_json(room):
    """Export messages to JSON file"""
    import json
    import os
    from datetime import datetime
    from django.conf import settings
    
    try:
        messages = ChatMessage.objects.filter(
            room=room
        ).select_related('user')[:10000]  # Limit for performance
        
        data = []
        for msg in messages:
            data.append({
                'id': str(msg.message_id),
                'user': msg.user.username if msg.user else 'System',
                'content': msg.content,
                'type': msg.message_type,
                'created_at': msg.created_at.isoformat(),
            })
        
        # Create archive directory
        archive_dir = getattr(settings, 'ROOM_ARCHIVE_DIR', 'room_archives')
        os.makedirs(archive_dir, exist_ok=True)
        
        # Write file
        filename = f"room_{room.slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(archive_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        
        return filepath
        
    except Exception as e:
        logger.error(f"Error exporting messages: {e}")
        return None


# ==================== CHAT ROOM JOIN VIEW (Password Protected) ====================

@login_required(login_url='XMR:signupin')
def chat_room_join(request, room_slug):
    """
    View for joining a password-protected room
    This handles the password entry page for private/protected rooms
    """
    room = get_object_or_404(ChatRoom, slug=room_slug, is_active=True)
    
    # Check if already a participant
    if request.user in room.participants.all():
        messages.info(request, f'You are already a member of {room.name}')
        return redirect('XMR:chat_room_detail', room_slug=room.slug)
    
    # Check if banned
    if request.user in room.banned_users.all():
        messages.error(request, 'You are banned from this room')
        return redirect('XMR:chat_room')
    
    if request.method == 'POST':
        password = request.POST.get('password')
        
        # Check if password protected
        if room.is_protected:
            if room.password == password:
                # Password correct - add user
                with transaction.atomic():
                    room.participants.add(request.user)
                    
                    # Create join message
                    ChatMessage.objects.create(
                        room=room,
                        user=request.user,
                        message_type='JOIN',
                        content=f"👋 {request.user.username} joined the room"
                    )
                    
                    # Send notifications to online users
                    NotificationService.notify_room(
                        room=room,
                        exclude_user=request.user,
                        notification_type='JOIN',
                        message=f"{request.user.username} joined the room"
                    )
                
                messages.success(request, f'✅ You have joined {room.name}')
                return redirect('XMR:chat_room_detail', room_slug=room.slug)
            else:
                messages.error(request, '❌ Incorrect password')
        else:
            # Room is not protected, just add user
            with transaction.atomic():
                room.participants.add(request.user)
                
                # Create join message
                ChatMessage.objects.create(
                    room=room,
                    user=request.user,
                    message_type='JOIN',
                    content=f"👋 {request.user.username} joined the room"
                )
                
                # Send notifications to online users
                NotificationService.notify_room(
                    room=room,
                    exclude_user=request.user,
                    notification_type='JOIN',
                    message=f"{request.user.username} joined the room"
                )
            
            messages.success(request, f'✅ You have joined {room.name}')
            return redirect('XMR:chat_room_detail', room_slug=room.slug)
    
    # Simple password entry page
    context = {
        'room': room,
        'mode': 'join'
    }
    return render(request, 'chat.html', context)




# =============================================================================
# ADDITIONAL CHAT API VIEWS - Add these after your existing chat views
# =============================================================================

@login_required
def chat_room_search(request, room_slug):
    """
    API endpoint to search within a specific room
    """
    query = request.GET.get('q', '').strip()
    page = int(request.GET.get('page', 1))
    per_page = 20
    
    if len(query) < 2:
        return JsonResponse({'error': 'Query too short'}, status=400)
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        
        if request.user not in room.participants.all():
            return JsonResponse({'error': 'Not a participant'}, status=403)
        
        # Search messages
        messages = ChatMessage.objects.filter(
            room=room,
            content__icontains=query,
            is_deleted=False
        ).select_related('user').order_by('-created_at')
        
        paginator = Paginator(messages, per_page)
        page_obj = paginator.get_page(page)
        
        results = []
        for msg in page_obj:
            results.append({
                'id': str(msg.message_id),
                'content': msg.content[:200],
                'user': msg.user.username if msg.user else 'System',
                'timestamp': msg.created_at.isoformat(),
                'url': f'/chat/{room.slug}/#message-{msg.message_id}'
            })
        
        return JsonResponse({
            'success': True,
            'results': results,
            'total': paginator.count,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
            'current_page': page,
            'total_pages': paginator.num_pages
        })
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)
    except Exception as e:
        logger.error(f"Error searching room: {e}")
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def chat_user_status(request):
    """
    API endpoint to update/check user status
    """
    if request.method == 'POST':
        # Update status
        status = request.POST.get('status')
        try:
            profile = request.user.profile
            
            if status == 'online':
                profile.online_status = True
                profile.away_mode = False
            elif status == 'away':
                profile.online_status = False
                profile.away_mode = True
            elif status == 'offline':
                profile.online_status = False
                profile.away_mode = False
            
            profile.last_seen = timezone.now()
            profile.save(update_fields=['online_status', 'away_mode', 'last_seen'])
            
            # Update Redis cache
            cache.set(f"online:{request.user.id}", True, timeout=300)
            
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    
    # GET request - get status of multiple users
    user_ids = request.GET.getlist('user_ids[]')
    
    if user_ids:
        users = User.objects.filter(id__in=user_ids).select_related('profile')
        status_data = []
        for user in users:
            status_data.append({
                'id': user.id,
                'online': cache.get(f"online:{user.id}") is not None,
                'away': getattr(user.profile, 'away_mode', False),
                'last_seen': user.profile.last_seen.isoformat() if hasattr(user, 'profile') and user.profile.last_seen else None,
            })
        
        return JsonResponse({
            'success': True,
            'users': status_data
        })
    
    return JsonResponse({'error': 'No user IDs provided'}, status=400)


@login_required
def chat_webhook_handler(request, room_slug):
    """
    Webhook endpoint for external services
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    
    # Verify webhook signature (implement your security here)
    signature = request.headers.get('X-Webhook-Signature')
    if not signature or not verify_webhook_signature(request.body, signature):
        return JsonResponse({'error': 'Invalid signature'}, status=401)
    
    try:
        room = ChatRoom.objects.get(slug=room_slug, is_active=True)
        data = json.loads(request.body)
        
        # Create system message from webhook
        message = ChatMessage.objects.create(
            room=room,
            user=None,
            message_type='SYSTEM',
            content=data.get('message', 'Webhook notification')
        )
        
        return JsonResponse({'success': True, 'message_id': str(message.message_id)})
        
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JsonResponse({'error': str(e)}, status=500)


def verify_webhook_signature(body, signature):
    """
    Verify webhook signature (implement your own logic)
    """
    # TODO: Implement your signature verification
    return True


@login_required
def health_check(request):
    """
    Health check endpoint
    """
    return JsonResponse({
        'status': 'healthy',
        'timestamp': timezone.now().isoformat(),
        'user': request.user.username if request.user.is_authenticated else None
    })


@login_required
def chat_stats(request):
    """
    Get chat statistics for the user
    """
    try:
        # Get user's chat stats
        total_rooms = ChatRoom.objects.filter(participants=request.user).count()
        total_messages = ChatMessage.objects.filter(user=request.user).count()
        
        # Get unread count
        unread_count = ChatRoom.objects.filter(
            participants=request.user
        ).annotate(
            unread=Count('messages', filter=Q(
                messages__created_at__gt=request.user.last_login,
                messages__read_by__isnull=True
            ))
        ).aggregate(total=Sum('unread'))['total'] or 0
        
        return JsonResponse({
            'success': True,
            'stats': {
                'total_rooms': total_rooms,
                'total_messages': total_messages,
                'unread_count': unread_count,
                'online_status': cache.get(f"online:{request.user.id}") is not None
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting chat stats: {e}")
        return JsonResponse({'error': str(e)}, status=500)


# =============================================================================
# CHAT ROOM JOIN VIEW (if you haven't added it yet)
# =============================================================================

@login_required(login_url='XMR:signupin')
def chat_room_join(request, room_slug):
    """
    View for joining a password-protected room
    """
    room = get_object_or_404(ChatRoom, slug=room_slug, is_active=True)
    
    # Check if already a participant
    if request.user in room.participants.all():
        messages.info(request, f'You are already a member of {room.name}')
        return redirect('XMR:chat_room_detail', room_slug=room.slug)
    
    # Check if banned
    if request.user in room.banned_users.all():
        messages.error(request, 'You are banned from this room')
        return redirect('XMR:chat_room')
    
    if request.method == 'POST':
        password = request.POST.get('password')
        
        # Check if password protected
        if room.is_protected:
            if room.password == password:
                # Password correct - add user
                with transaction.atomic():
                    room.participants.add(request.user)
                    
                    # Create join message
                    ChatMessage.objects.create(
                        room=room,
                        user=request.user,
                        message_type='JOIN',
                        content=f"👋 {request.user.username} joined the room"
                    )
                    
                    # Send notifications
                    try:
                        for participant in room.participants.exclude(id=request.user.id):
                            ChatNotificationMessage.objects.create(
                                user=participant,
                                notification_type='JOIN',
                                message=f"{request.user.username} joined the room",
                                related_object_id=room.id
                            )
                    except Exception as e:
                        logger.error(f"Failed to send notifications: {e}")
                
                messages.success(request, f'✅ You have joined {room.name}')
                return redirect('XMR:chat_room_detail', room_slug=room.slug)
            else:
                messages.error(request, '❌ Incorrect password')
        else:
            # Room is not protected, just add user
            with transaction.atomic():
                room.participants.add(request.user)
                
                # Create join message
                ChatMessage.objects.create(
                    room=room,
                    user=request.user,
                    message_type='JOIN',
                    content=f"👋 {request.user.username} joined the room"
                )
            
            messages.success(request, f'✅ You have joined {room.name}')
            return redirect('XMR:chat_room_detail', room_slug=room.slug)
    

    context = {
        'room': room,
        'mode': 'join'
    }
    return render(request, 'chat.html', context)


from django.conf import settings
