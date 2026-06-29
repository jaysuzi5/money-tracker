from django.core.management.base import BaseCommand

from tracker.models import Category

DEFAULT_TREE = {
    'Income': ['Salary', 'Interest', 'Dividends', 'Refund', 'Other Income'],
    'Auto': ['Fuel', 'Service', 'Registration', 'Parking'],
    'Utilities': ['Electric', 'Gas', 'Water', 'Trash', 'Internet', 'Phone',
                  'Streaming', 'Propane'],
    'Home': ['Mortgage', 'Rent', 'Repairs', 'Furnishings', 'Lawn', 'Supplies',
             'Appliances'],
    'Food': ['Groceries', 'Dining', 'Coffee'],
    'Health': ['Doctor', 'Dental', 'Pharmacy', 'Vision'],
    'Shopping': ['Clothing', 'Electronics', 'Household', 'Gifts'],
    'Personal': ['Allowance', 'Subscriptions', 'Hobbies', 'Education'],
    'Insurance': ['Auto', 'Home', 'Life', 'Health'],
    'Taxes': ['Federal', 'State', 'City', 'County', 'School', 'Property'],
    'Financial': ['Bank Fees', 'Interest Paid', 'Investments'],
    'Travel': ['Lodging', 'Airfare', 'Transportation', 'Vacation'],
    'Giving': ['Charity', 'Christmas'],
    'Misc': ['Uncategorized', 'Other'],
}


class Command(BaseCommand):
    help = 'Seed a default Quicken-style category tree (idempotent).'

    def handle(self, *args, **opts):
        created = 0
        for parent_name, children in DEFAULT_TREE.items():
            parent, made = Category.objects.get_or_create(parent=None, name=parent_name)
            created += int(made)
            for child_name in children:
                _, made = Category.objects.get_or_create(parent=parent, name=child_name)
                created += int(made)
        total = Category.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Seed complete. Created {created} new categories ({total} total).'))
