import os
import re
import tempfile

from django import forms
from django.conf import settings
from django.forms import ModelForm

import happyforms
import Image
from easy_thumbnails import processors
from statsd import statsd
from tower import ugettext as _, ugettext_lazy as _lazy

from phonebook.models import Invite
from groups.models import Group
from users.models import User, UserProfile


PAGINATION_LIMIT = 20

REGEX_NUMERIC = re.compile('\d+', re.IGNORECASE)


class SearchForm(happyforms.Form):
    q = forms.CharField(widget=forms.HiddenInput, required=True)
    limit = forms.CharField(widget=forms.HiddenInput, required=False)
    nonvouched_only = forms.BooleanField(required=False)

    def clean_limit(self):
        """Validate that this limit is numeric and greater than 1"""
        limit = self.cleaned_data['limit']

        if not limit:
            limit = PAGINATION_LIMIT
        elif not REGEX_NUMERIC.match(str(limit)) or int(limit) < 1:
            limit = PAGINATION_LIMIT

        return limit


class ProfileForm(forms.ModelForm):
    first_name = forms.CharField(label=_lazy(u'First Name'), max_length=30,
                                                             required=False)
    last_name = forms.CharField(label=_lazy(u'Last Name'), max_length=30,
                                                           required=True)

    photo = forms.ImageField(label=_lazy(u'Profile Photo'), required=False)
    photo_delete = forms.BooleanField(label=_lazy(u'Remove Profile Photo'),
                                      required=False)

    # Remote System Ids
    # Tightly coupled with larper.UserSession.form_to_service_ids_attrs
    groups = forms.CharField(label=_lazy(u'Groups'), required=False)

    #: L10n: Street address; not entire address
    street = forms.CharField(label=_lazy(u'Address'), required=False)
    city = forms.CharField(label=_lazy(u'City'), required=False)
    # TODO: Add validation of states/provinces/etc. for known/large countries.
    province = forms.CharField(label=_lazy(u'Province/State'), required=False)
    # TODO: Add list of countries.
    country = forms.CharField(label=_lazy(u'Country'), required=False)
    postal_code = forms.CharField(label=_lazy(u'Postal/Zip Code'),
                                  required=False)

    class Meta:
        model = UserProfile
        fields = ('ircname', 'website', 'bio')
        widgets = {
            'bio': forms.Textarea(),
        }

    def clean_photo(self):
        """Let's make sure things are right.

        Cribbed from zamboni. Thanks Dave Dash!

        TODO: this needs to go into celery

        - File IT bug for celery
        - Ensure files aren't InMemory files
        - See zamboni.apps.users.forms
        """
        photo = self.cleaned_data['photo']

        if not photo:
            return

        if photo.content_type not in ('image/png', 'image/jpeg'):
            raise forms.ValidationError(
                    _('Images must be either PNG or JPG.'))

        if photo.size > settings.MAX_PHOTO_UPLOAD_SIZE:
            raise forms.ValidationError(
                    _('Please use images smaller than %dMB.' %
                      (settings.MAX_PHOTO_UPLOAD_SIZE / 1024 / 1024 - 1)))

        im = Image.open(photo)
        # Resize large images
        if any(d > 300 for d in im.size):
            im = processors.scale_and_crop(im, (300, 300), crop=True)
        fn = tempfile.mktemp(suffix='.jpg')
        f = open(fn, 'w')
        im.save(f, 'JPEG')
        f.close()
        photo.file = open(fn)
        return photo

    def clean_groups(self):
        """Groups are saved in lowercase because it's easy and consistent."""
        if not re.match(r'^[a-zA-Z0-9 .:,-]*$', self.cleaned_data['groups']):
            raise forms.ValidationError(_(u'Groups can only contain '
                                           'alphanumeric characters, dashes, '
                                           'spaces.'))

        return [g.strip() for g in (self.cleaned_data['groups']
                                        .lower().split(','))
                if g and ',' not in g]

    def save(self, request):
        """Save the data to profile."""
        self._save_groups(request)
        self._save_photos(request)

        d = self.cleaned_data
        user = request.user
        user.first_name = d['first_name']
        user.last_name = d['last_name']
        super(ModelForm, self).save()
        user.save()

    def _save_groups(self, request):
        """Parse a string of (usually comma-demilited) groups and save them."""
        profile = request.user.get_profile()

        # Remove any non-system groups that weren't supplied in this list.
        profile.groups.remove(*[g for g in profile.groups.all()
                                if g.name not in self.cleaned_data['groups']
                                and not g.system])

        # Add/create the rest of the groups
        groups_to_add = []
        for g in self.cleaned_data['groups']:
            (group, created) = Group.objects.get_or_create(name=g)

            if not group.system:
                groups_to_add.append(group)

        profile.groups.add(*groups_to_add)

    def _save_photos(self, request):
        d = self.cleaned_data
        profile = request.user.get_profile()

        if d['photo_delete']:
            profile.photo = False
            try:
                os.remove(profile.get_photo_file())
            except OSError:
                statsd.incr('errors.photo.deletion')
        elif d['photo']:
            profile.photo = True
            with open(profile.get_photo_file(), 'w') as f:
                f.write(d['photo'].file.read())

        profile.save()


class VouchForm(happyforms.Form):
    """Vouching is captured via a user's id."""
    vouchee = forms.IntegerField(widget=forms.HiddenInput)


class InviteForm(happyforms.ModelForm):

    def clean_recipient(self):
        recipient = self.cleaned_data['recipient']

        if User.objects.filter(email=recipient).count() > 0:
            raise forms.ValidationError(_(u'You cannot invite someone who has '
                                            'already been vouched.'))
        return recipient

    def save(self, inviter):
        invite = super(InviteForm, self).save(commit=False)
        invite.inviter = inviter
        invite.save()
        return invite

    class Meta:
        model = Invite
        exclude = ('redeemer', 'inviter')
